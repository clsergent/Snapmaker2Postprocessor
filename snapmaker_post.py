#!/usr/bin/env python3
import argparse
import base64
import datetime
import os
import pathlib
import re
# A FreeCAD postprocessor for the Snapmaker 2.0 CNC function
# made by clsergent
# licence is EUPL 1.2

import sys
import tempfile
from typing import Sequence, Any

import FreeCAD
import Path
import Path.Post.Processor
import Path.Post.UtilsArguments
import Path.Post.UtilsExport
import Path.Post.Utils
import Path.Post.UtilsParse
import Path.Main.Job
from matplotlib.image import thumbnail

translate = FreeCAD.Qt.translate

if DEBUG := False:
    Path.Log.setLevel(Path.Log.Level.DEBUG, Path.Log.thisModule())
    Path.Log.trackModule(Path.Log.thisModule())
else:
    Path.Log.setLevel(Path.Log.Level.INFO, Path.Log.thisModule())

SNAPMAKER_MACHINES = dict(
    original=dict(name='Snapmaker Original', X=90, Y=90, Z=50),
    original_z_extension=dict(name='Snapmaker Original with Z extension', X=90, Y=90, Z=146),
    a150=dict(name='A150', X=160, Y=160, Z=90),
    **dict.fromkeys(('A250', 'A250T'), dict(name='Snapmaker 2 A250(T)', X=230, Y=250, Z=180)),
    **dict.fromkeys(('A350', 'A350T'), dict(name='Snapmaker 2 A350(T)', X=320, Y=350, Z=275)),
    artisan=dict(name='Snapmaker Artisan', X=400, Y=400, Z=400)
)

SNAPMAKER_TOOLHEADS = ('50W', '200W')

class CoordinatesAction(argparse.Action):
    """argparse Action to handle coordinates x,y,z"""
    def __call__(self, parser, namespace, values, option_string):
        match = re.match('^ *(-?\d+\.?\d*),? *(-?\d+\.?\d*),? *(-?\d+\.?\d*) *$', values)
        if match:
            # setattr(namespace, self.dest, 'G0 X{0} Y{1} Z{2}'.format(*match.groups()))
            params = {key: float(value) for key, value in zip(("X", "Y", "Z"), match.groups())}
            setattr(namespace, self.dest, params)
        else:
            raise argparse.ArgumentError(None, message='invalid coordinates provided')


class Snapmaker(Path.Post.Processor.PostProcessor):
    """FreeCAD postprocessor for Snapmaker CNC function"""
    def __init__(self, job) -> None:
        super().__init__(
            job=job,
            tooltip=translate("CAM", "Snapmaker post processor"),
            tooltipargs=[""],
            units="Metric",
        )

        self.values: dict[str, Any] = dict()
        self.argument_defaults: dict[str, bool] = dict()
        self.arguments_visible: dict[str, bool] = dict()
        self.parser = argparse.ArgumentParser()

        self.init_values()
        self.init_argument_defaults()
        self.init_arguments_visible()
        self.parser = self.init_parser(self.values, self.argument_defaults, self.arguments_visible)

        # create another parser with all visible arguments
        all_arguments_visible = dict()
        for key in iter(self.arguments_visible):
            all_arguments_visible[key] = True
        self.visible_parser = self.init_parser(self.values, self.argument_defaults, all_arguments_visible)

        FreeCAD.Console.PrintLog(f'{self.values["POSTPROCESSOR_FILE_NAME"]}: initialized.')

    def init_values(self):
        """Initialize values that are used throughout the postprocessor."""
        Path.Post.UtilsArguments.init_shared_values(self.values)

        # shared values
        self.values["POSTPROCESSOR_FILE_NAME"] = __name__
        self.values["COMMENT_SYMBOL"] = ';'
        self.values["ENABLE_MACHINE_SPECIFIC_COMMANDS"] = True
        self.values["END_OF_LINE_CHARACTERS"] = '\n'
        self.values["FINISH_LABEL"] = "End"
        self.values["LINE_INCREMENT"] = 1
        self.values["MACHINE_NAME"] = 'Generic Snapmaker'
        self.values["MODAL"] = True
        self.values["OUTPUT_PATH_LABELS"] = True
        self.values["OUTPUT_HEADER"] = True  # remove FreeCAD standard header and use a custom Snapmaker Header
        self.values["OUTPUT_TOOL_CHANGE"] = True
        self.values["PARAMETER_ORDER"] = ["X", "Y", "Z", "A", "B", "C", "I", "J", "F",
                                          "S", "T", "Q", "R", "L", "H", "D", "P", "O"]
        self.values["PREAMBLE"] = f"""G90\nG17"""
        self.values["PRE_OPERATION"] = """"""
        self.values["POST_OPERATION"] = """"""
        self.values["POSTAMBLE"] = """M400\nM5"""
        self.values["SHOW_MACHINE_UNITS"] = False
        self.values["SPINDLE_DECIMALS"] = 0  # TODO: update spindle from rpm to percent
        self.values["SPINDLE_WAIT"] = 4.0
        self.values["TOOL_CHANGE"] = "M76"  # handle tool change by inserting an HMI pause
        self.values["TRANSLATE_DRILL_CYCLES"] = True  # drill cycle gcode must be translated
        self.values["USE_TLO"] = False  # G43 is not handled. TODO: check that nothing has to be added

        # snapmaker values
        self.values["THUMBNAIL"] = True
        self.values["MACHINES"] = SNAPMAKER_MACHINES
        self.values["TOOLHEADS"] = SNAPMAKER_TOOLHEADS
        self.values["TOOLHEAD_NAME"] = None
        self.values["BOUNDARIES"] = dict(X=-1, Y=-1, Z=-1)

    def init_argument_defaults(self) -> None:
        """Initialize which arguments (in a pair) are shown as the default argument."""
        Path.Post.UtilsArguments.init_argument_defaults(self.argument_defaults)

        self.argument_defaults["tlo"] = False
        self.argument_defaults["translate-drill"] = True

        # snapmaker arguments
        self.argument_defaults["thumbnail"] = True
        self.argument_defaults["gui"] = True
        self.argument_defaults["boundaries-check"] = False

    def init_arguments_visible(self) -> None:
        """Initialize which argument pairs are visible in TOOLTIP_ARGS."""
        Path.Post.UtilsArguments.init_arguments_visible(self.arguments_visible)

        self.arguments_visible["axis-modal"] = False
        self.arguments_visible["header"] = False
        self.arguments_visible["return-to"] = True
        self.arguments_visible["tlo"] = False
        self.arguments_visible["tool_change"] = True
        self.arguments_visible["translate-drill"] = False
        self.arguments_visible["wait-for-spindle"] = True

        # snapmaker arguments (for record, always visible)
        self.arguments_visible["thumbnail"] = True
        self.arguments_visible["gui"] = True
        self.arguments_visible["boundaries-check"] = True
        self.arguments_visible["machine"] = True
        self.arguments_visible["toolhead"] = True

    def init_parser(self, values, argument_defaults, arguments_visible) -> argparse.ArgumentParser:
        """Initialize the postprocessor arguments parser"""
        parser = Path.Post.UtilsArguments.init_shared_arguments(values, argument_defaults, arguments_visible)

        # snapmaker custom arguments
        group = parser.add_argument_group("Snapmaker only arguments")
        # add_flag_type_arguments function is not used as its behavior is inconsistent with argparse
        # handle thumbnail generation
        group.add_argument('--thumbnail', action='store_true', default=argument_defaults["thumbnail"],
                           help='include a thumbnail (require --gui)')
        group.add_argument('--no-thumbnail', action='store_false', dest='thumbnail', help='remove thumbnail')

        group.add_argument('--gui', action='store_true', default=argument_defaults["gui"],
                           help='allow the postprocessor to execute GUI methods')
        group.add_argument('--no-gui', action='store_false', dest='gui',
                           help='execute postprocessor without requiring GUI')

        group.add_argument('--boundaries-check', action='store_true', default=argument_defaults["boundaries-check"],
                           help='check boundaries according to the machine build area')
        group.add_argument('--no-boundaries-check', action='store_false', dest='boundaries_check',
                           help='disable boundaries check')

        group.add_argument("--boundaries", default=None, type=CoordinatesAction,
                           help='Custom boundaries (e.g. "100, 200, 300"). Overrides --machine',)

        group.add_argument('--machine', default=None, choices=self.values["MACHINES"].keys(),
                          help='Snapmaker machine version')

        group.add_argument('--toolhead', default=self.values["TOOLHEAD_NAME"], choices=self.values["TOOLHEADS"],
                          help='Snapmaker toolhead')

        return parser

    def process_arguments(self, filename: str = '-') -> (bool, str | argparse.Namespace):
        """Process any arguments to the postprocessor."""
        (flag, args) = Path.Post.UtilsArguments.process_shared_arguments(
            self.values, self.parser, self._job.PostProcessorArgs, self.visible_parser, filename
        )
        if flag:  # process extra arguments only if flag is True
            if args.machine:
                self.values["MACHINE_NAME"] = self.values["MACHINES"][args.machine]['name']

                self.values["BOUNDARIES"] = {key: self.values["MACHINES"][args.machine][key] for key in ('X','Y','Z')}

            if args.boundaries:  # may override machine boundaries, which is expected
                self.values["BOUNDARIES"] = args.boundaries

            if args.toolhead:
                self.values["TOOLHEAD_NAME"] = args.toolhead.upper()
            else:
                self.values["TOOLHEAD_NAME"] = self.values["TOOLHEADS"][0]
                FreeCAD.Console.PrintWarning(f'No toolhead selected, using default ({self.values["TOOLHEAD_NAME"]})\n'
                                             f'Consider adding --toolhead')

            self.values["THUMBNAIL"] = args.thumbnail
            self.values["ALLOW_GUI"] = args.gui

        return flag, args

    def process_postables(self, filename: str = '-') -> [(str, str)]:
        """process job sections to gcode"""
        sections: [(str, str)] = list()

        postables = self._buildPostList()

        # basic filename handling. TODO: enhance this section
        if len(postables) > 1 and filename != '-':
            filename = pathlib.Path(filename)
            filename = str(filename.with_stem(filename.stem + '_{name}'))

        for name, objects in postables:
            print(f'name is {name}, filename is {filename}')
            gcode = self.export_common(objects, filename.format(name=name))
            sections.append((name, gcode))

        return sections

    def get_job(self, objects) -> Path.Main.Job.ObjectJob | None:
        """get the path job from the postprocessed objects"""
        job = None
        for obj in objects:  # get job from objects
            try:
                return obj.Proxy.getJob(obj)
            except AttributeError:
                FreeCAD.Console.PrintLog(f'No parent job was found for {obj}\n')
        else:
            if self.values["ALLOW_GUI"] and FreeCAD.GuiUp:  # get job from selection
                import FreeCADGui
                jobs = []
                for selection in FreeCADGui.Selection.getSelection():
                    if hasattr(selection, "Proxy") and isinstance(selection.Proxy, Path.Main.Job.ObjectJob):
                        jobs.append(selection)

                if len(jobs) > 0:
                    if len(jobs) > 1:
                        FreeCAD.Console.PrintWarning('Only one job should be selected, using the first one\n')
                    return jobs[0]
                else:
                    FreeCAD.Console.PrintError('Failed to find a Path job. Select a Path job\n')
            else:  # TODO: get job from document if GUI not up (job can be retrieved using Path.Main.Job.Instances())
                FreeCAD.Console.PrintError('Failed to find a Path job. Consider adding --gui\n')

    def get_thumbnail(self, job) -> str:
        """generate a thumbnail of the job from the given objects"""
        if self.values["THUMBNAIL"] is False:
            return ''

        if job is None:
            FreeCAD.Console.PrintError('No valid Path job provided: thumbnail generation skipped\n')
            return ''

        if not (self.values["ALLOW_GUI"] and FreeCAD.GuiUp):
            FreeCAD.Console.PrintError('GUI access required: thumbnail generation skipped. Consider adding --gui\n')
            return ''

        # get FreeCAD references
        import FreeCADGui
        view = FreeCADGui.activeDocument().activeView()
        selection = FreeCADGui.Selection

        # save current selection
        selected = [obj.Object for obj in selection.getCompleteSelection() if hasattr(obj, 'Object')]
        selection.clearSelection()

        # clear view
        FreeCADGui.runCommand('Std_SelectAll', 0)
        all = []
        for obj in selection.getCompleteSelection():
            if hasattr(obj, 'Object'):
                all.append((obj.Object, obj.Object.Visibility))
                obj.Object.ViewObject.hide()

        # select models to display
        for model in job.Model.Group:
            model.ViewObject.show()
            selection.addSelection(model.Document.Name, model.Name)
        view.fitAll()  # center selection
        view.viewIsometric()  # display as isometric
        selection.clearSelection()

        # generate thumbnail
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, 'thumbnail.png')
            view.saveImage(path, 720, 480, 'Transparent')
            with open(path, 'rb') as file:
                data = file.read()

        # restore view
        for obj, visibility in all:
            if visibility:
                obj.ViewObject.show()

        # restore selection
        for obj in selected:
            selection.clearSelection()
            selection.addSelection(obj.Document.Name, obj.Name)

        return f'thumbnail: data:image/png;base64,{base64.b64encode(data).decode()}'

    def output_header(self, gcode: [[]], job: Path.Main.Job.ObjectJob):
        """custom method derived from Path.Post.UtilsExport.output_header"""
        cam_file: str
        comment: str
        nl: str = "\n"

        if not self.values["OUTPUT_HEADER"]:
            return

        def add_comment(text):
            comment = Path.Post.UtilsParse.create_comment(self.values, text)
            gcode.append(f'{Path.Post.UtilsParse.linenumber(self.values)}{comment}{self.values["END_OF_LINE_CHARACTERS"]}')

        add_comment('Header Start')
        add_comment('header_type: cnc')
        add_comment(f'machine: {self.values["MACHINE_NAME"]}')
        comment = Path.Post.UtilsParse.create_comment(
            self.values, f'Post Processor: {self.values["POSTPROCESSOR_FILE_NAME"]}'
        )
        gcode.append(f"{Path.Post.UtilsParse.linenumber(self.values)}{comment}{nl}")
        if FreeCAD.ActiveDocument:
            cam_file = os.path.basename(FreeCAD.ActiveDocument.FileName)
        else:
            cam_file = "<None>"
        add_comment(f'Cam File: {cam_file}')
        add_comment(f'Output Time: {datetime.datetime.now()}')
        add_comment(self.get_thumbnail(job))

    def export_common(self, objects: list, filename: str | pathlib.Path) -> str:
        """custom method derived from Path.Post.UtilsExport.export_common"""
        final: str
        gcode: [[]] = []
        result: bool

        for obj in objects:
            if not hasattr(obj, "Path"):
                print(f"The object {obj.Name} is not a path.")
                print("Please select only path and Compounds.")
                return ""

        Path.Post.UtilsExport.check_canned_cycles(self.values)
        if self.values["OUTPUT_HEADER"]:
            job = self.get_job(objects)
            self.output_header(gcode, job)
        Path.Post.UtilsExport.output_safetyblock(self.values, gcode)
        Path.Post.UtilsExport.output_tool_list(self.values, gcode, objects)
        Path.Post.UtilsExport.output_preamble(self.values, gcode)
        Path.Post.UtilsExport.output_motion_mode(self.values, gcode)
        Path.Post.UtilsExport.output_units(self.values, gcode)

        for obj in objects:
            # Skip inactive operations
            if hasattr(obj, "Active") and not obj.Active:
                continue
            if hasattr(obj, "Base") and hasattr(obj.Base, "Active") and not obj.Base.Active:
                continue
            coolant_mode = Path.Post.UtilsExport.determine_coolant_mode(obj)
            Path.Post.UtilsExport.output_start_bcnc(self.values, gcode, obj)
            Path.Post.UtilsExport.output_preop(self.values, gcode, obj)
            Path.Post.UtilsExport.output_coolant_on(self.values, gcode, coolant_mode)
            # output the G-code for the group (compound) or simple path
            Path.Post.UtilsParse.parse_a_group(self.values, gcode, obj)
            Path.Post.UtilsExport.output_postop(self.values, gcode, obj)
            Path.Post.UtilsExport.output_coolant_off(self.values, gcode, coolant_mode)

        Path.Post.UtilsExport.output_return_to(self.values, gcode)
        #
        # This doesn't make sense to me.  It seems that both output_start_bcnc and
        # output_end_bcnc should be in the for loop or both should be out of the
        # for loop.  However, that is the way that grbl post code was written, so
        # for now I will leave it that way until someone has time to figure it out.
        #
        Path.Post.UtilsExport.output_end_bcnc(self.values, gcode)
        Path.Post.UtilsExport.output_postamble_header(self.values, gcode)
        Path.Post.UtilsExport.output_tool_return(self.values, gcode)
        Path.Post.UtilsExport.output_safetyblock(self.values, gcode)
        Path.Post.UtilsExport.output_postamble(self.values, gcode)

        final = "".join(gcode)

        if FreeCAD.GuiUp and self.values["SHOW_EDITOR"]:
            # size limit removed as irrelevant on my computer - see if issues occur
            dia = Path.Post.Utils.GCodeEditorDialog()
            dia.editor.setText(final)
            result = dia.exec_()
            if result:
                final = dia.editor.toPlainText()

        return final

    def export(self, filename: str | pathlib.Path = '-'):
        """process gcode and export"""
        (flag, args) = self.process_arguments()
        if flag:
            return self.process_postables(filename)
        else:
            return [("allitems", args)]

    @property
    def tooltip(self) -> str:
        tooltip = "Postprocessor of the FreeCAD CAM workbench for the Snapmaker machines"
        return tooltip

    @property
    def tooltipArgs(self) -> str:
        return self.parser.format_help()

    @property
    def units(self) -> str:
        return self._units


if __name__ == '__main__':
    Snapmaker(None).visible_parser.format_help()