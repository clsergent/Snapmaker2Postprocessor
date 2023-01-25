# A FreeCAD postprocessor for the Snapmaker 2.0 CNC function

import os
import re
import argparse
import shlex
import timeit
from datetime import datetime
import base64
import tempfile

import FreeCAD
import Path
import PathScripts.PathUtil as PathUtil
import PathScripts.PostUtils as PostUtils
import PathScripts.PathJob as PathJob

__version__ = '1.0.6'
__author__ = 'clsergent'
__license__ = 'EUPL1.2'

# Immutable values
GCODE_MOTION_MODE = "G90"  # G90 - Absolute moves
GCODE_WORK_PLANE = "G17"  # G17 only, XY plane, for vertical milling


# Default config values
MACHINE = None  # machine in use (for boundary check)
UNITS = 'mm'
PRECISION = 3  # Decimal places displayed for metric
MAX_SPINDLE_SPEED = 12000   # Max rpm spindle speed (value for Snapmaker 2.0 CNC module)
MIN_SPINDLE_SPEED = 6000    # Min rpm spindle speed (value for Snapmaker 2.0 CNC module)
TRANSLATE_DRILL_CYCLES = True  # If true, G81, G82, and G83 are translated, ignored otherwise
DRILL_RETRACT_MODE = "G98"  # End of drill-cycle retraction type. G99 is the alternative (require TRANSLATE_DRILL_CYCLES)
TOOL_CHANGE = True  # if True, insert a tool change (M25). May also be a custom gcode
SPINDLE_WAIT = 0  # Time in seconds to wait after M3 M4 M5 (0 means until all commands are done = M400)
PAUSE = "M76"  # pause command
REMOVE_DUPLICATES = True  # True: Commands are suppressed if they are the same as the previous line
LINE_START = 1  # Line number starting value
LINE_INCREMENT = 1  # Line number increment
BOUNDARIES_CHECK = True

# File options
INCLUDE_HEADER = True  # Output header in output gcode file
INCLUDE_THUMBNAIL = True  # Add a PNG thumbnail in header
INCLUDE_COMMENTS = True  # Comments in output gcode file
INCLUDE_LINE_NUMBERS = False  # Output line numbers in output gcode file
INCLUDE_TOOL_NUMBER = False  # include tool number change (TXX), unsupported by Snapmaker, but may be used in simulation

# Machine options
# https://snapmaker.com/snapmaker-original/specs
# https://snapmaker.com/snapmaker-2/specs
BOUNDARIES = dict(original=(90, 90, 50),
                  original_z_extension=(90, 90, 146),
                  **dict.fromkeys(('A150',), (160, 160, 90)),
                  **dict.fromkeys(('A250', 'A250T'), (230, 250, 180)),
                  **dict.fromkeys(('A350', 'A350T'), (320, 350, 275)),
                  )

# FreeCAD GUI options
SHOW_EDITOR = True  # Display the resulting gcode file

# GCODE optional commands
GCODE_PREAMBLE = ""  # Text inserted at the beginning of the gcode output file.
GCODE_POSTAMBLE = "M400\nM5"  # Text inserted after the last operation
GCODE_PRE_OPERATION = ''  # text inserted before every operation
GCODE_POST_OPERATION = ''  # Post operation text will be inserted after every operation
GCODE_FINAL_POSITION = None  # None = No movement at end of program

# GCODE commands
GCODE_UNITS = {'mm': "G21", 'in': "G20"}
GCODE_COOLANT = {'mist': "F7", 'flood': "M8", 'off': "M9"}
GCODE_COMMANDS = ["G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03", "G4", "G04", "G17", "G21", "G28", "G54", "G80",
                  "G90", "M3", "M03", "M4", "M04", "M5", "M05", "M17", "M18", "M25", "M76", "M81"]
GCODE_PARAMETERS = ["X", "Y", "Z", "A", "B", "C", "I", "J", "F", "S", "T", "Q", "R", "L", "H", "D", "P", "O"]
GCODE_COMMENT_SYMBOLS = (';', '')   # start and end of comments signs
GCODE_PAUSE = ("M25", "M76")  # M6 not handled by marlin
GCODE_SPACER = " "

TOOLTIP = 'Snapmaker 2.0 CNC postprocessor for FreeCAD'


def getSelectedJob() -> PathJob.ObjectJob:
    """return the selected job"""
    # job can be retrieved using selection or through PathScripts.PathJob.Instances()
    if FreeCAD.GuiUp:
        import FreeCADGui
        jobs = []
        for selection in FreeCADGui.Selection.getSelection():
            if hasattr(selection, "Proxy") and isinstance(selection.Proxy, PathJob.ObjectJob):
                jobs.append(selection)

        if len(jobs) > 0:
            if len(jobs) > 1:
                FreeCAD.Console.PrintWarning('Only one job should be selected, using the first one\n')
            return jobs[0]
    else:  # TODO: get job from document if GUI not up
        FreeCAD.Console.PrintError('No job can be found by selection without GUI\n')
    return None


def getJob(obj) -> PathJob.ObjectJob:
    """return the parent job of the provided object"""
    try:
        return obj.Proxy.getJob(obj)
    except AttributeError:
        FreeCAD.Console.PrintLog(f'No parent job was found for {obj}\n')
        return None


def getThumbnail(job) -> str:
    """generate a thumbnail of the job"""
    if FreeCAD.GuiUp:
        import FreeCADGui
        selection = FreeCADGui.Selection.getCompleteSelection()
        FreeCADGui.Selection.clearSelection()

        # select models to display
        for model in job.Model.Group:
            model.ViewObject.show()
            FreeCADGui.Selection.addSelection(model.Document.Name, model.Name)
        FreeCADGui.runCommand('Std_ViewFitSelection', 0)  # center selection
        FreeCADGui.activeDocument().activeView().viewIsometric()  # display as isometric
        FreeCADGui.Selection.clearSelection()

        for obj in selection:  # restore selection
            if hasattr(obj, 'Object'):
                obj = obj.Object
            FreeCADGui.Selection.addSelection(obj.Document.Name, obj.Name)

        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, 'thumbnail.png')
            FreeCADGui.activeDocument().activeView().saveImage(path, 720, 480, 'Transparent')
            with open(path, 'rb') as file:
                data = file.read()

        return f'thumbnail: data:image/png;base64,{base64.b64encode(data).decode()}'
    else:
        return ''


def convertPosition(position: float, units=UNITS) -> float:
    """convert FreeCAD position value according to the given unit"""
    return float(FreeCAD.Units.Quantity(position, FreeCAD.Units.Length).getValueAs(units))


def convertSpeed(speed: float, units=UNITS) -> float:
    """convert FreeCAD speed value according to the given unit"""
    return float(FreeCAD.Units.Quantity(speed, FreeCAD.Units.Velocity).getValueAs(f'{units}/min'))


def speedAsPercent(speed: float) -> int:
    """return spindle speed (rpm) as percentage (Snapmaker specific)"""
    return int(max(min(speed, MAX_SPINDLE_SPEED), MIN_SPINDLE_SPEED) * 100 // MAX_SPINDLE_SPEED)


def getRapidSpeeds(obj: Path = None, job=None) -> (float, float):
    """Return rapid speeds"""
    if obj is not None and hasattr(obj, "ToolController"):
        vRapidSpeed, hRapidSpeed = obj.ToolController.VertRapid, obj.ToolController.HorizRapid
    elif job is not None:
        vRapidSpeed, hRapidSpeed = job.SetupSheet.VertRapid, job.SetupSheet.HorizRapid
    else:
        FreeCAD.Console.PrintWarning('No Rapid speeds (vertical and horizontal) set for the selected job\n')
        vRapidSpeed, hRapidSpeed = None, None
    return vRapidSpeed, hRapidSpeed


class Comment(str):
    symbols = GCODE_COMMENT_SYMBOLS

    def __str__(self):
        return self.symbols[0] + self + self.symbols[-1]


class Header(Comment):
    pass


class Command:
    units = UNITS
    precision = PRECISION
    spacer = GCODE_SPACER

    def __init__(self, name, **parameters):
        if type(name) is Path.Command:
            self._cmd = name
        else:
            self._cmd = Path.Command(name, parameters)
    
    @property
    def Name(self) -> str:
        return self._cmd.Name
    
    @property
    def Parameters(self) -> dict:
        return self._cmd.Parameters

    def addParameter(self, parameter, value: str | int | float = ''):
        """add a parameter to the command"""
        self._cmd.setFromGCode(f'{self._cmd.toGCode()} {parameter}{value}')

    def __eq__(self, other) -> bool:
        try:
            return self._cmd.Name == other.Name and self._cmd.Parameters == other.Parameters
        except AttributeError:
            return False

    def __str__(self) -> str:
        line = [self._cmd.Name]
        for param in GCODE_PARAMETERS:
            if param not in self._cmd.Parameters:
                continue

            # Position parameters
            elif param in ("X", "Y", "Z", "U", "V", "W", "I", "J", "K", "R", "Q"):
                line.append(f'{param}{convertPosition(self._cmd.Parameters[param], self.units):.{self.precision}f}')

            # Speed parameters
            elif param == 'F':
                speed = convertSpeed(self._cmd.Parameters[param], self.units)
                line.append(f'{param}{speed:.{self.precision}f}')
                if speed <= 0:
                    FreeCAD.Console.PrintError(f'{self._cmd.Name}: negative or null speed provided ({speed})\n')

            elif param == "S":
                # Spindle speed handling (Snapmaker uses spindle speed in percent rather than rpm)
                if self._cmd.Name in ("M3", "M03", "M4", "M04"):
                    line.append(f'P{speedAsPercent(self._cmd.Parameters[param]):.{self.precision}f}')
                elif self._cmd.Name in ("G4", "G04"):
                    line.append(f'{param}{self._cmd.Parameters[param]:.{self.precision}f}')
                else:
                    line.append(f'{param}{convertSpeed(self._cmd.Parameters[param], self.units):.{self.precision}f}')

            # String parameter
            elif param in ("T", "H", "D", "P", "L"):
                line.append(f'{param}{self._cmd.Parameters[param]}')

            # Numeric parameters
            elif param in ("A", "B", "C"):
                line.append(f'{param}{self._cmd.Parameters[param]}')

        return self.spacer.join(line)


class Gcode(list):
    def __init__(self, iterable=(), *, configuration: argparse.Namespace):
        list.__init__(self, iterable)
        self.conf = configuration

        # line types to include
        self.types = {Command, str}
        if self.conf.header:
            self.types.add(Header)
        if self.conf.comments:
            self.types.add(Comment)

        # line format settings
        Command.units = self.conf.units
        Command.precision = self.conf.precision
        Command.spacer = self.conf.spacer
        Comment.symbols = self.conf.comment_symbols

        # "current" commands values, may be altered
        self.drillRetractMode = Command(DRILL_RETRACT_MODE)

    def lastCommand(self, *names, start=-1, default=None) -> Command:
        """return the last command amongst names"""
        if len(self) == 0:
            return default
        for i in range(start % len(self), 0, -1):
            if type(self[i]) is Command and self[i].Name in names:
                return self[i]
            return default

    def lastParameter(self, param, *names, start=-1, default=None):
        """return the last parameter with given name. Command name may be limited by names"""
        if len(self) == 0:
            return default
        for i in range(start % len(self), 0, -1):
            if type(self[i]) is Command and (not names or self[i].Name in names) and param in self[i].Parameters:
                try:
                    return self[i].Parameters[param]
                except KeyError:
                    return default
            return default

    def append(self, line):
        if self.conf.remove_duplicates and len(self) and self[-1] == line:
            return
        else:
            list.append(self, line)

    def __str__(self) -> str:
        """Export gcode as string"""
        lines = []
        nbr = 0
        for line in self:
            if type(line) in self.types:
                if self.conf.line_numbers and type(line) in (Command, str):
                    lines.append(f'N{self.conf.line_start + nbr * self.conf.line_increment}{self.conf.spacer}{line}')
                    nbr += 1
                else:
                    lines.append(str(line))
        return '\n'.join(lines)


class CoordinatesAction(argparse.Action):
    """argparse Action to handle coordinates x,y,z"""
    def __call__(self, parser, namespace, values, option_string):
        match = re.match('^ *(\d+\.\d{0,3}),? *(\d+\.\d{0,3}),? *(\d+\.\d{0,3}) *$', values)
        if match:
            # setattr(namespace, self.dest, 'G0 X{0} Y{1} Z{2}'.format(*match.groups()))
            params = {key: float(value) for key, value in zip(("X", "Y", "Z"), match.groups())}
            setattr(namespace, self.dest, Command("G0", **params))
        else:
            raise argparse.ArgumentError(None, message='invalid coordinates provided')


class Postprocessor:
    def __init__(self):
        self.configure()
        self.gcode = Gcode(configuration=self.conf)
        self.job = None
    
    def configure(self, *args):
        """set postprocessor values"""
        parser = argparse.ArgumentParser(prog='Snapmaker_2_CNC_post',
                                         description='Snapmaker 2.0 CNC postprocessor for FreeCAD')

        parser.add_argument('--header', action='store_true', default=INCLUDE_HEADER, help='include header')
        parser.add_argument('--no-header', action='store_false', dest='header', help='remove header')

        parser.add_argument('--comments', action='store_true', default=INCLUDE_COMMENTS, help='include comments')
        parser.add_argument('--no-comments', action='store_false', dest='comments', help='remove comments')
        parser.add_argument('--comment-symbols', nargs=2, type=str, default=GCODE_COMMENT_SYMBOLS,
                            help='comment symbols')

        parser.add_argument('--thumbnail', action='store_true', default=INCLUDE_THUMBNAIL,
                            help='include a thumbnail (require --header')
        parser.add_argument('--no-thumbnail', action='store_false', dest='thumbnail',
                            help='remove thumbnail')

        parser.add_argument('--line-numbers', action='store_true', default=INCLUDE_LINE_NUMBERS,
                            help='prefix with line numbers')
        parser.add_argument('--no-line-numbers', action='store_false', dest='line_numbers',
                            help='do not prefix with line numbers')

        parser.add_argument('--line-start', type=int, default=LINE_START,
                            help='first line number')
        parser.add_argument('--line-increment', type=int, default=LINE_INCREMENT,
                            help='line number increment')
        
        parser.add_argument('--remove-duplicates', action='store_true', default=REMOVE_DUPLICATES,
                            help='remove duplicate lines')
        parser.add_argument('--keep-duplicates', action='store_false', dest='remove_duplicates',
                            help='keep duplicate lines')
        
        parser.add_argument('--show-editor', action='store_true', default=SHOW_EDITOR,
                            help='pop up editor before writing output')
        parser.add_argument('--no-show-editor', action='store_false', dest='show_editor',
                            help='do not pop up editor before writing output')

        parser.add_argument('--precision', type=int, default=PRECISION, help='number of digits of precision')

        parser.add_argument('--pause', choices=GCODE_PAUSE, default=PAUSE, help=f'pause command to use')

        parser.add_argument('--units', choices=GCODE_UNITS.keys(), default=UNITS, help='unit in use')
        
        parser.add_argument('--preamble', default=GCODE_PREAMBLE, help='commands to be issued before the first command')
        parser.add_argument('--postamble', default=GCODE_POSTAMBLE, help='commands to be issued after the last command')
        
        parser.add_argument('--pre-operation', default=GCODE_PRE_OPERATION,
                            help='commands to be issued before each operation')
        parser.add_argument('--post-operation', default=GCODE_POST_OPERATION,
                            help='commands to be issued after each operation')
        
        parser.add_argument('--translate-drill-cycles', action='store_true', default=TRANSLATE_DRILL_CYCLES,
                            help='convert drill cycles (G81, G82, and G83)')
        parser.add_argument('--no-translate-drill-cycles', action='store_false', dest='translate_drill_cycle',
                            help='ignore drill cycles (G81, G82, and G83)')

        parser.add_argument('--tool-change', nargs='?', const=TOOL_CHANGE, default=TOOL_CHANGE,
                            help='insert tool change gcode (optional gcode may be provided)')
        parser.add_argument('--no-tool-change', action='store_false', dest='tool_change', help='remove tool change gcode')

        parser.add_argument('--tool-number', action='store_true', default=INCLUDE_TOOL_NUMBER,
                            help='insert tool number gcode TXX (unsupported by Snapmaker but may be used for simulation)')
        parser.add_argument('--no-tool-number', action='store_false', dest='tool_change', help='remove tool number gcode')

        parser.add_argument('--spindle-wait', type=int, default=SPINDLE_WAIT,
                            help='wait for spindle to reach desired speed after M3 or M4')
        
        parser.add_argument('--spacer', type=str, default=GCODE_SPACER, help='space character(s) in use')
        
        parser.add_argument('--commands', action='extend', nargs='+', default=GCODE_COMMANDS,
                            help='allow additional commands')

        parser.add_argument('--final-position', action=CoordinatesAction, default=GCODE_FINAL_POSITION,
                            help='Position to reach at the end of work (i.e. "3.175, 4.702, 50.915")')

        parser.add_argument('--machine', choices=BOUNDARIES.keys(), default=MACHINE,
                            help='machine name (for boundary check)')

        parser.add_argument('--boundaries-check', action='store_true', default=BOUNDARIES_CHECK,
                            help='check boundaries according to the machine build area')
        parser.add_argument('--no-boundaries-check', action='store_false', dest='boundaries_check',
                            help='disable boundaries check')

        self.conf = parser.parse_args(args=args)

    def addCommand(self, name, *, obj: Path = None, **parameters):
        cmd = Command(name, **parameters)
        self.gcode.append(cmd)

        if cmd.Name in ("G0", "G00") and "F" not in cmd.Parameters:
            vRapidSpeed, hRapidSpeed = getRapidSpeeds(obj, self.job)
            if hRapidSpeed is not None and ("X" in cmd.Parameters or "Y" in cmd.Parameters):
                if "Z" in cmd.Parameters:
                    cmd.addParameter("F", float(min(vRapidSpeed, hRapidSpeed)))
                else:
                    cmd.addParameter("F", float(hRapidSpeed))
            elif "Z" in cmd.Parameters and vRapidSpeed is not None:
                cmd.addParameter("F", float(vRapidSpeed))

    def translateDrill(self, cmd, obj: Path) -> list:
        """Translate canned drill cycles
        Cycle conversion only converts the cycles in the XY plane (G17).
        ZX (G18) and YZ (G19) planes produce false gcode."""

        drillX = cmd.Parameters["X"]  # FreeCAD.Units.Quantity(cmd.Parameters["X"], FreeCAD.Units.Length)
        drillY = cmd.Parameters["Y"]  # FreeCAD.Units.Quantity(cmd.Parameters["Y"], FreeCAD.Units.Length)
        drillZ = cmd.Parameters["Z"]  # FreeCAD.Units.Quantity(cmd.Parameters["Z"], FreeCAD.Units.Length)
        drillR = cmd.Parameters["R"]  # FreeCAD.Units.Quantity(cmd.Parameters["R"], FreeCAD.Units.Length)
        drillF = cmd.Parameters["F"]  # FreeCAD.Units.Quantity(cmd.Parameters["F"], FreeCAD.Units.Velocity)

        position = {param: self.gcode.lastParameter(param, default=0) for param in ("X", "Y", "Z")}

        if drillR < drillZ:
            FreeCAD.Console.PrintError(f'Drill cycle error: R less than Z\n')
            return []

        # set retract Z
        if self.gcode.drillRetractMode == "G98" and position['Z'] > drillR:
            retractZ = position['Z']
        else:
            retractZ = drillR

        # retract if necessary
        if position['Z'] < retractZ:
            self.addCommand("G0", Z=retractZ, obj=obj)

        # Move to XY hole
        if position["X"] != drillX and position["Y"] != drillY:
            self.addCommand("G0", X=drillX, Y=drillY, obj=obj)

        self.addCommand("G0", Z=drillR, obj=obj)

        if cmd.Name == "G81":
            self.addCommand("G1", Z=drillZ, F=drillF)

        elif cmd.Name == "G82":
            self.addCommand("G1", Z=drillZ, F=drillF)
            self.addCommand("G4", S=cmd.Parameters['P'])

        elif cmd.Name == "G83":
            drillStep = cmd.Parameters["Q"]  # FreeCAD.Units.Quantity(cmd.Parameters["Q"], FreeCAD.Units.Length)
            chipSpace = drillStep * 0.5
            nextStopZ = drillR - drillStep
            while nextStopZ >= drillZ:
                self.addCommand("G1", Z=nextStopZ, F=drillF)

                if (nextStopZ - drillStep) >= drillZ:
                    self.addCommand("G0", Z=drillR, obj=obj)
                    self.addCommand("G0", Z=nextStopZ + chipSpace, obj=obj)
                    nextStopZ -= drillStep
                elif nextStopZ == drillZ:
                    break
                else:
                    self.addCommand("G0", Z=drillR, obj=obj)
                    self.addCommand("G0", Z=nextStopZ + chipSpace, obj=obj)
                    self.addCommand("G1", Z=drillZ, F=drillF)
                    break
        self.addCommand("G0", Z=retractZ, obj=obj)

    def parseObject(self, obj) -> Gcode:
        # Group of objects
        if hasattr(obj, 'Group'):
            self.gcode.append(Comment(f'GROUP: {obj.Label}'))
            for item in obj.Group:
                self.gcode.append(Comment(f'PATH: {item.Label}'))
                self.parseObject(item)
            return self.gcode

        # Ignore non Path objects
        if not hasattr(obj, 'Path'):
            return self.gcode

        FreeCAD.Console.PrintLog(f'Processing object {obj.Name}\n')

        for cmd in obj.Path.Commands:
            # Allowed commands
            if cmd.Name in self.conf.commands:
                self.addCommand(cmd, obj=obj)

            else:
                # Set drill retraction mode
                if cmd.Name in ("G98", "G99") and self.conf.translate_drill_cycles is True:
                    self.gcode.drillRetractMode = cmd.Name

                # Convert drill cycles
                elif cmd.Name in ("G81", "G82", "G83") and self.conf.translate_drill_cycles is True:
                    self.translateDrill(cmd, obj)

                # Messages
                elif cmd.Name == 'message':
                    self.gcode.append(Comment(f'message: {cmd}'))

                # Comments
                elif self.conf.comments and (match := re.match('^\((.+)\)$', cmd.Name)):
                    self.gcode.append(Comment(match.groups()[0]))

                # Ignore unknown commands
                else:
                    FreeCAD.Console.PrintWarning(f'Command ignored: {cmd.Name}\n')

                continue

            # Post command operations
            # Add Wait for spindle speed
            if cmd.Name in ("M3", "M03", "M4", "M04"):
                if self.conf.spindle_wait > 0:
                    self.addCommand("G4", S=int(self.conf.spindle_wait))
                else:
                    self.addCommand("G4")

            # Tool change: add custom gcode or pause
            elif cmd.Name in ("M6", "M06") and self.conf.tool_change:
                self.gcode.append(Comment(f'TOOL CHANGE'))

                # use custom gcode if provided
                if type(self.conf.tool_change) is str:
                    for line in self.conf.tool_change.splitlines():
                        self.gcode.append(line)

                # fallback to pause
                else:
                    self.addCommand(self.conf.pause)

        return self.gcode

    def checkBoundaries(self) -> bool:
        """check boundaries and return whether it succeeded"""
        FreeCAD.Console.PrintLog('Boundaries check/n')

        if self.conf.machine not in BOUNDARIES.keys():
            FreeCAD.Console.PrintError(f'Boundary check failed, no valid machine name supplied')
            return False

        boundaries = dict(X=[0, 0], Y=[0, 0], Z=[0, 0])
        position = dict(X=0, Y=0, Z=0)
        relative = False

        for cmd in self.gcode:
            if type(cmd) is Command:
                if cmd.Name == 'G90':
                    relative = False
                elif cmd.Name == 'G91':
                    relative = False
                elif cmd.Name in ('G0', 'G1'):
                    for axis in boundaries.keys():
                        if (value := cmd.Parameters.get(axis)) is not None:
                            if relative:
                                position[axis] += value
                            else:
                                position[axis] = value
                            boundaries[axis][0] = max(boundaries[axis][0], position[axis])
                            boundaries[axis][1] = min(boundaries[axis][1], position[axis])

        for axis, limit in zip(boundaries.keys(), BOUNDARIES[self.conf.machine]):
            if abs(boundaries[axis][0] - boundaries[axis][1]) > limit:
                FreeCAD.Console.PrintWarning(f'Boundary check: job exceeds machine limit on {axis} axis\n')

    def export(self, objects, filename: str, argstring: str):
        FreeCAD.Console.PrintMessage(f'Post Processor: {__name__}\nPostprocessing...\n')

        if argstring:
            self.configure(*shlex.split(argstring))
            self.gcode = Gcode(configuration=self.conf)

        for obj in objects:
            if job := getJob(obj):
                self.job = job
                break
        if self.job is None:
            self.job = getSelectedJob()

        if self.job is None:
            FreeCAD.Console.PrintError(f'no job was found, please select a job before calling the postprocessor\n')

        self.gcode.append(Header('Header Start'))
        self.gcode.append(Header('Exported by FreeCAD'))
        self.gcode.append(Header(f'Postprocessor: {__name__}'))
        self.gcode.append(Header(f'Output Time: {datetime.now()}'))
        if self.conf.thumbnail and (thumbnail := getThumbnail(self.job)):
            self.gcode.append(Header(thumbnail))
        self.gcode.append(Header('Header End'))
        
        # Preamble gcode
        self.gcode.append(Comment('PREAMBLE'))
        for line in self.conf.preamble.splitlines():
            self.gcode.append(line)
        
        # Configuration (after preamble to avoid overwriting)
        self.gcode.append(Comment('CONFIGURATION'))
        self.addCommand(GCODE_MOTION_MODE)
        self.addCommand(GCODE_UNITS[self.conf.units])
        self.addCommand(GCODE_WORK_PLANE)

        tool = None
        for obj in objects:
            # Skip invalid objects
            if not hasattr(obj, 'Path'):
                FreeCAD.Console.PrintWarning(f'Object {obj.Name} is not a valid Path. Please select only Paths and Compounds\n')
                continue
            
            # Skip inactive objects
            if PathUtil.opProperty(obj, "Active") is False:
                FreeCAD.Console.PrintWarning(f'Object {obj.Name} is inactive and will be skipped\n')
                continue

            # Insert pause to change tool if required
            if hasattr(obj, 'ToolController'):
                if obj.ToolController.FullName != tool and tool is not None:
                    self.gcode.append(Comment(f'TOOL CHANGE: {tool}'))
                    self.addCommand(self.conf.pause)
                tool = obj.ToolController.FullName
                if self.conf.tool_number:
                    # not Command(...) because unsupported by Snapmaker
                    self.gcode.append(f'T{obj.ToolController.ToolNumber:02n}')

            # Pre-operation gcode
            self.gcode.append(Comment(f'OPERATION: {obj.Label}'))
            for line in self.conf.pre_operation.splitlines():
                self.gcode.append(line)
            
            # Coolant on
            if hasattr(obj, 'CoolantMode'):
                coolantMode = obj.CoolantMode
            elif hasattr(obj, 'Base') and hasattr(obj.Base, 'CoolantMode'):
                coolantMode = obj.Base.CoolantMode
            else:
                coolantMode = 'None'    # None is the default value returned by the obj
            
            if coolantMode != 'None':
                self.gcode.append(Comment(f'COOLANT ON: {coolantMode}'))
                self.addCommand(GCODE_COOLANT[coolantMode.lower()])
            
            # Object commands
            self.parseObject(obj)
            
            # Post operation gcode
            self.gcode.append(Comment(f'END OF OPERATION: {obj.Label}'))
            for line in self.conf.post_operation.splitlines():
                self.gcode.append(line)
            
            # Coolant Off
            if coolantMode != 'None':
                self.gcode.append(Comment(f'COOLANT OFF: {coolantMode}'))
                self.addCommand(GCODE_COOLANT['off'])

        # Final position
        if self.conf.final_position:
            self.gcode.append(self.conf.final_position)

        # Postamble gcode
        self.gcode.append(Comment('POSTAMBLE'))
        for line in self.conf.postamble.splitlines():
            self.gcode.append(line)
        
        FreeCAD.Console.PrintMessage(f'Postprocessing done\n')

        # boundaries check
        if self.conf.boundaries_check:
            self.checkBoundaries()

        # Show editor
        data = str(self.gcode)
        if FreeCAD.GuiUp and self.conf.show_editor:
            dialog = PostUtils.GCodeEditorDialog()
            dialog.editor.setText(data)
            result = dialog.exec_()
            if result:
                data = dialog.editor.toPlainText()
        
        # Export to file
        with open(filename, 'w') as file:
            file.write(data)


def export(objects, filename: str, argstring: str):
    post = Postprocessor()
    post.export(objects, filename, argstring)


if __name__ == '__main__':
    raise Warning('this module is not intended to be used standalone')
