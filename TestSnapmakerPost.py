#!/usr/bin/env python3
# ***************************************************************************
# *  Copyright (c) 2025 Clair-Loup Sergent <clsergent@free.fr>              *
# *                                                                         *
# *  Licensed under the EUPL-1.2 with the specific provision                *
# *  (EUPL articles 14 & 15) that the applicable law is the French law.     *
# *  and the Jurisdiction Paris.                                            *
# *  Any redistribution must include the specific provision above.          *
# *                                                                         *
# *  You may obtain a copy of the Licence at:                               *
# *  https://joinup.ec.europa.eu/software/page/eupl5                        *
# *                                                                         *
# *  Unless required by applicable law or agreed to in writing, software    *
# *  distributed under the Licence is distributed on an "AS IS" basis,      *
# *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or        *
# *  implied. See the Licence for the specific language governing           *
# *  permissions and limitations under the Licence.                         *
# ***************************************************************************
import re

import FreeCAD

import Path
import CAMTests.PathTestUtils as PathTestUtils
from Path.Post.Processor import PostProcessorFactory

Path.Log.setLevel(Path.Log.Level.DEBUG, Path.Log.thisModule())
Path.Log.trackModule(Path.Log.thisModule())


class TestSnapmakerPost(PathTestUtils.PathTestBase):
    """Test the refactored_grbl_post.py postprocessor."""

    @classmethod
    def setUpClass(cls):
        """setUpClass()...

        This method is called upon instantiation of this test class.  Add code
        and objects here that are needed for the duration of the test() methods
        in this class.  In other words, set up the 'global' test environment
        here; use the `setUp()` method to set up a 'local' test environment.
        This method does not have access to the class `self` reference, but it
        is able to call static methods within this same class.
        """

        FreeCAD.ConfigSet("SuppressRecomputeRequiredDialog", "True")
        cls.doc = FreeCAD.open(FreeCAD.getHomePath() + "/Mod/CAM/CAMTests/boxtest.fcstd")
        cls.job = cls.doc.getObject("Job")
        cls.post = PostProcessorFactory.get_post_processor(cls.job, "snapmaker")
        # locate the operation named "Profile"
        for op in cls.job.Operations.Group:
            if op.Label == "Profile":
                # remember the "Profile" operation
                cls.profile_op = op
                return

    @classmethod
    def tearDownClass(cls):
        """tearDownClass()...

        This method is called prior to destruction of this test class.  Add
        code and objects here that cleanup the test environment after the
        test() methods in this class have been executed.  This method does not
        have access to the class `self` reference.  This method
        is able to call static methods within this same class.
        """
        FreeCAD.closeDocument(cls.doc.Name)
        FreeCAD.ConfigSet("SuppressRecomputeRequiredDialog", "")

    # Setup and tear down methods called before and after each unit test

    def setUp(self):
        """setUp()...

        This method is called prior to each `test()` method.  Add code and
        objects here that are needed for multiple `test()` methods.
        """
        # allow a full length "diff" if an error occurs
        self.maxDiff = None
        # reinitialize the postprocessor data structures between tests
        self.post.initialize()

    def tearDown(self):
        """tearDown()...

        This method is called after each test() method. Add cleanup instructions here.
        Such cleanup instructions will likely undo those in the setUp() method.
        """
        pass

    def test_general(self):
        """ Test Output Generation """

        # generate an empty path
        self.profile_op.Path = Path.Path([])

        expected_header = """\
;Header Start
;header_type: cnc
;machine: Snapmaker 2 A350(T)
;Post Processor: Snapmaker_post
;Cam File: boxtest.fcstd
;Output Time: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{0,6}
;thumbnail: deactivated."""

        expected_body = """\
;Begin preamble
G90
G17
G21
;Begin operation: Fixture
;Path: Fixture
G54
;End operation: Fixture
;Begin operation: TC: Default Tool
;Path: TC: Default Tool
;TC: Default Tool
;Begin toolchange
M5
M76
M6 T1
;End operation: TC: Default Tool
;Begin operation: Profile
;Path: Profile
;End operation: Profile
;Begin postamble
M400
M5
"""

        # test header and body with comments
        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=50W\
                                      --spindle-percent"
        gcode = self.post.export()[0][1]

        g_lines = gcode.splitlines()
        e_lines = expected_header.splitlines() + expected_body.splitlines()

        self.assertTrue(len(g_lines), len(e_lines))
        for (nbr, exp), line in zip(enumerate(e_lines), g_lines):
            if exp.startswith(';Output Time:'):
                self.assertTrue(re.match(exp, line) is not None)
            else:
                self.assertTrue(line, exp)

        self.profile_op.Path = Path.Path([])

        # test body without header
        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=50W\
                                      --spindle-percent --no-header"
        gcode = self.post.export()[0][1]
        self.assertEqual(gcode, expected_body)

        # test body without comments
        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=50W\
                                      --spindle-percent --no-header --no-comments"
        gcode = self.post.export()[0][1]
        expected = ''.join([line for line in expected_body.splitlines(keepends=True) if not line.startswith(';')])
        self.assertEqual(gcode, expected)

    def test_command(self):
        """Test command Generation """

        c = Path.Command("G0 X10 Y20 Z30")
        self.profile_op.Path = Path.Path([c])

        # test G0 command
        expected = "G0 X10.000 Y20.000 Z30.000"
        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=50W\
                                      --spindle-percent --no-header"
        gcode = self.post.export()[0][1]
        result = gcode.splitlines()[18]
        self.assertEqual(result, expected)

    def test_precision(self):
        """Test Precision"""
        c = Path.Command("G0 X10 Y20 Z30")
        self.profile_op.Path = Path.Path([c])

        # test G0 command with precision 2 digits precision
        expected = "G0 X10.00 Y20.00 Z30.00"
        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=50W\
                                      --spindle-percent --no-header --precision=2"
        gcode = self.post.export()[0][1]
        result = gcode.splitlines()[18]
        self.assertEqual(result, expected)

    def test_lines(self):
        """ Test Line Numbers """
        expected = "N46 G0 X10.000 Y20.000 Z30.000"
        c = Path.Command("G0 X10 Y20 Z30")
        self.profile_op.Path = Path.Path([c])

        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=50W\
                                      --spindle-percent --no-header --line-numbers --line-number=10 --line-increment=2"
        gcode = self.post.export()[0][1]
        result = gcode.splitlines()[18]
        self.assertEqual(result, expected)

    def test_preamble(self):
        """ Test Pre-amble """

        self.profile_op.Path = Path.Path([])

        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=50W\
                                      --spindle-percent --no-header --preamble='G18 G55' --no-comments"
        gcode = self.post.export()[0][1]
        result = gcode.splitlines()[0]
        self.assertEqual(result, "G18 G55")

    def test_postamble(self):
        """ Test Post-amble """
        self.profile_op.Path = Path.Path([])

        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=50W\
                                      --spindle-percent --no-header --postamble='G0 Z50\nM2' --no-comments"
        gcode = self.post.export()[0][1]
        result = gcode.splitlines()[-2]
        self.assertEqual(result, "G0 Z50")
        self.assertEqual(gcode.splitlines()[-1], "M2")

    def test_inches(self):
        """ Test inches conversion """

        c = Path.Command("G0 X10 Y20 Z30")

        self.profile_op.Path = Path.Path([c])

        # test inches conversion
        expected = "G0 X0.3937 Y0.7874 Z1.1811"
        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=50W\
                                     --spindle-percent --no-header --inches"
        gcode = self.post.export()[0][1]
        self.assertEqual(gcode.splitlines()[3], "G20")
        result = gcode.splitlines()[18]
        self.assertEqual(result, expected)

        # test inches conversion with 2 digits precision
        expected = "G0 X0.39 Y0.79 Z1.18"
        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=50W\
                                      --spindle-percent --no-header --inches --precision=2"
        gcode = self.post.export()[0][1]
        result = gcode.splitlines()[18]

        self.assertEqual(result, expected)

    def test_axis_modal(self):
        """ Test axis modal - Suppress the axis coordinate if the same as previous """

        c0 = Path.Command("G0 X10 Y20 Z30")
        c1 = Path.Command("G0 X10 Y30 Z30")
        self.profile_op.Path = Path.Path([c0, c1])

        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=50W\
                                      --spindle-percent --no-header --axis-modal"
        gcode = self.post.export()[0][1]
        result = gcode.splitlines()[19]
        expected = "G0 Y30.000"
        self.assertEqual(result, expected)

    def test_tool_change(self):
        """ Test tool change """

        c0 = Path.Command("M6 T2")
        c1 = Path.Command("M3 S3000")
        self.profile_op.Path = Path.Path([c0, c1])

        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=50W\
                                      --spindle-percent --no-header"
        gcode = self.post.export()[0][1]
        print(gcode)
        self.assertEqual(gcode.splitlines()[19:22], ["M5", "M76", "M6 T2"])
        self.assertEqual(gcode.splitlines()[22], "M3 P25")

        # suppress TLO
        self.job.PostProcessorArgs = "--no-header --no-tlo --no-show-editor"
        gcode = self.post.export()[0][1]
        print(gcode)
        self.assertEqual(gcode.splitlines()[17], "M3 S3000")

    def test_spindle(self):
        """ Test spindle speed conversion from RPM to percents """

        c = Path.Command("M3 S3600")
        self.profile_op.Path = Path.Path([c])

        # test 50W toolhead
        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=50W\
                                      --no-header"
        gcode = self.post.export()[0][1]
        print(gcode)
        self.assertEqual(gcode.splitlines()[18], "M3 P30")

        # test 200W toolhead
        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=200W\
                                      --no-header --spindle-percent"
        gcode = self.post.export()[0][1]
        print(gcode)
        self.assertEqual(gcode.splitlines()[18], "M3 P20")

        # test custom spindle speed extrema
        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=200W\
                                              --no-header --spindle-percent --spindle-speeds=3000,4000"
        gcode = self.post.export()[0][1]
        print(gcode)
        self.assertEqual(gcode.splitlines()[18], "M3 P90")

    def test_comment(self):
        """ Test comment """

        c = Path.Command("(comment)")

        self.profile_op.Path = Path.Path([c])

        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=50W\
                                      --no-header"
        gcode = self.post.export()[0][1]
        result = gcode.splitlines()[18]
        expected = ";comment"
        self.assertEqual(result, expected)

    def test_boundaries(self):
        """ Test boundaries check """

        # check succeeds
        c = Path.Command("G0 X100 Y-100.5 Z-1")
        self.profile_op.Path = Path.Path([c])

        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=50W\
                                      --no-header --boundaries-check"
        gcode = self.post.export()[0][1]
        self.assertTrue(self.post.check_boundaries(gcode.splitlines()))

        # check fails with A350
        c0 = Path.Command("G01 X100 Y-100.5 Z-1")
        c1 = Path.Command("G02 Y260")
        self.profile_op.Path = Path.Path([c0, c1])

        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=A350 --toolhead=50W\
                                      --no-header --boundaries-check"
        gcode = self.post.export()[0][1]
        self.assertFalse(self.post.check_boundaries(gcode.splitlines()))

        # check succeed with artisan (which base is bigger)
        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=artisan --toolhead=50W\
                                      --no-header --boundaries-check"
        gcode = self.post.export()[0][1]
        self.assertTrue(self.post.check_boundaries(gcode.splitlines()))

        # check fails with custom boundaries
        self.job.PostProcessorArgs = "--no-show-editor --no-gui --no-thumbnail --machine=artisan --toolhead=50W\
                                      --no-header --boundaries-check --boundaries='50,400,10'"
        gcode = self.post.export()[0][1]
        self.assertFalse(self.post.check_boundaries(gcode.splitlines()))

