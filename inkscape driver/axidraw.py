# coding=utf-8
#
# Copyright 2022 Windell H. Oskay, Evil Mad Scientist Laboratories
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

"""
axidraw.py

Part of the AxiDraw driver for Inkscape
https://github.com/evil-mad/AxiDraw

See version_string below for current version and date.

Requires Python 3.7 or newer and Pyserial 3.5 or newer.
"""

import copy
import gettext
from importlib import import_module
import logging
import math
import time
from array import array
# from multiprocessing import Event

from lxml import etree

from axidrawinternal.axidraw_options import common_options, versions

from axidrawinternal.plot_utils_import import from_dependency_import # plotink
simplepath = from_dependency_import('ink_extensions.simplepath')
simplestyle = from_dependency_import('ink_extensions.simplestyle')
cubicsuperpath = from_dependency_import('ink_extensions.cubicsuperpath')
simpletransform = from_dependency_import('ink_extensions.simpletransform')
inkex = from_dependency_import('ink_extensions.inkex')
exit_status = from_dependency_import('ink_extensions_utils.exit_status')
message = from_dependency_import('ink_extensions_utils.message')
ebb_serial = from_dependency_import('plotink.ebb_serial')  # https://github.com/evil-mad/plotink
ebb_motion = from_dependency_import('plotink.ebb_motion')
plot_utils = from_dependency_import('plotink.plot_utils')
text_utils = from_dependency_import('plotink.text_utils')
requests = from_dependency_import('requests')

from axidrawinternal import path_objects
from axidrawinternal import digest_svg
from axidrawinternal import boundsclip
from axidrawinternal import plot_optimizations
from axidrawinternal import plot_status
from axidrawinternal import pen_handling
from axidrawinternal import plot_warnings
from axidrawinternal import serial_utils
# from axidrawinternal import preview

logger = logging.getLogger(__name__)

class AxiDraw(inkex.Effect):
    """ Main class for AxiDraw """

    logging_attrs = {"default_handler": message.UserMessageHandler()}

    def __init__(self, default_logging=True, user_message_fun=message.emit, params=None):
        if params is None:
            params = import_module("axidrawinternal.axidraw_conf") # Default configuration file
        self.params = params

        inkex.Effect.__init__(self)

        self.OptionParser.add_option_group(
            common_options.core_options(self.OptionParser, params.__dict__))
        self.OptionParser.add_option_group(
            common_options.core_mode_options(self.OptionParser, params.__dict__))

        self.version_string = "3.7.0" # Dated 2022-10-20

        self.plot_status = plot_status.PlotStatus()
        self.pen = pen_handling.PenHandler()
        self.warnings = plot_warnings.PlotWarnings()
        # self.preview = preview.Preview()

        self.spew_debugdata = False # Possibly add this as a PlotStatus variable
        self.pathcount = int(0)
        self.set_defaults()

        self.connected = False # Python API variable.

        self.Secondary = False # Possibly add this as a PlotStatus variable
        self.user_message_fun = user_message_fun

        self.start_x = None
        self.start_y = None
        self.end_x = None
        self.end_y = None

        if default_logging: # logging setup
            logger.setLevel(logging.INFO)
            logger.addHandler(self.logging_attrs["default_handler"])

        if self.spew_debugdata:
            logger.setLevel(logging.DEBUG) # by default level is INFO

    def set_up_pause_receiver(self, software_pause_event):
        """ use a multiprocessing.Event/threading.Event to communicate a
        keyboard interrupt (ctrl-C) to pause the AxiDraw """
        self._software_pause_event = software_pause_event

    def receive_pause_request(self):
        return hasattr(self, "_software_pause_event") and self._software_pause_event.is_set()

    def set_secondary(self, suppress_standard_out=True):
        """ Various things are slightly different if this is a "secondary"
        AxiDraw called by axidraw_control """
        self.Secondary = True
        self.called_externally = True
        if suppress_standard_out:
            self.suppress_standard_output_stream()

    def suppress_standard_output_stream(self):
        """ Save values we will need later in unsuppress_standard_output_stream """
        self.logging_attrs["additional_handlers"] = [SecondaryErrorHandler(self),\
            SecondaryNonErrorHandler(self)]
        self.logging_attrs["emit_fun"] = self.user_message_fun
        logger.removeHandler(self.logging_attrs["default_handler"])
        for handler in self.logging_attrs["additional_handlers"]:
            logger.addHandler(handler)

    def unsuppress_standard_output_stream(self):
        """ Release logging stream """
        logger.addHandler(self.logging_attrs["default_handler"])
        if self.logging_attrs["additional_handlers"]:
            for handler in self.logging_attrs["additional_handlers"]:
                logger.removeHandler(handler)

        self.user_message_fun = self.logging_attrs["emit_fun"]

    def set_defaults(self):
        """ Set default values of certain parameters
            These are set when the class is initialized.
            Also called in plot_run(), to ensure that
            these defaults are set before plotting additional pages."""

        self.svg_layer_old = int(-2)
        self.svg_node_count_old = int(0)
        self.svg_last_path_old = int(0)
        self.svg_last_path_nc_old = int(0)
        self.svg_last_known_x_old = float(0.0)
        self.svg_last_known_y_old = float(0.0)
        self.svg_paused_x_old = float(0.0)
        self.svg_paused_y_old = float(0.0)
        self.svg_rand_seed_old = int(1)
        self.svg_row_old = int(0)
        self.svg_application_old = None
        self.svg_plob_version = None
        self.use_layer_speed = False
        self.node_count = 0
        self.pathcount = int(0)
        self.plot_status.reset() # Clear serial port and pause status flags
        self.pen.reset() # Clear virtual pen state, lift count, layer pen height flag
        self.warnings.reset() # Clear any warning messages

        self.x_bounds_min = 0.0
        self.y_bounds_min = 0.0
        self.svg_transform = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]

    def update_options(self):
        """ Parse and update certain options; called in effect and in interactive modes
            whenever the options are updated """

        # Physical travel bounds, based on AxiDraw model:
        if self.options.model == 2:
            self.x_bounds_max = self.params.x_travel_V3A3
            self.y_bounds_max = self.params.y_travel_V3A3
        elif self.options.model == 3:
            self.x_bounds_max = self.params.x_travel_V3XLX
            self.y_bounds_max = self.params.y_travel_V3XLX
        elif self.options.model == 4:
            self.x_bounds_max = self.params.x_travel_MiniKit
            self.y_bounds_max = self.params.y_travel_MiniKit
        elif self.options.model == 5:
            self.x_bounds_max = self.params.x_travel_SEA1
            self.y_bounds_max = self.params.y_travel_SEA1
        elif self.options.model == 6:
            self.x_bounds_max = self.params.x_travel_SEA2
            self.y_bounds_max = self.params.y_travel_SEA2
        elif self.options.model == 7:
            self.x_bounds_max = self.params.x_travel_V3B6
            self.y_bounds_max = self.params.y_travel_V3B6
        else:
            self.x_bounds_max = self.params.x_travel_default
            self.y_bounds_max = self.params.y_travel_default

        self.bounds = [[self.x_bounds_min - 1e-9, self.y_bounds_min - 1e-9],
                       [self.x_bounds_max + 1e-9, self.y_bounds_max + 1e-9]]

        self.x_max_phy = self.x_bounds_max  # Copy for physical limit reference
        self.y_max_phy = self.y_bounds_max

        # Speeds in inches/second:
        self.speed_pendown = self.params.speed_pendown * self.params.speed_lim_xy_hr / 110.0
        self.speed_penup = self.params.speed_penup * self.params.speed_lim_xy_hr / 110.0

        # Input limit checking; constrain input values and prevent zero speeds:
        self.options.pen_pos_up = plot_utils.constrainLimits(self.options.pen_pos_up, 0, 100)
        self.options.pen_pos_down = plot_utils.constrainLimits(self.options.pen_pos_down, 0, 100)
        self.options.pen_rate_raise = \
            plot_utils.constrainLimits(self.options.pen_rate_raise, 1, 200)
        self.options.pen_rate_lower = \
            plot_utils.constrainLimits(self.options.pen_rate_lower, 1, 200)
        self.options.speed_pendown = plot_utils.constrainLimits(self.options.speed_pendown, 1, 110)
        self.options.speed_penup = plot_utils.constrainLimits(self.options.speed_penup, 1, 200)
        self.options.accel = plot_utils.constrainLimits(self.options.accel, 1, 110)
        self.pen.update(self.options, self.params)


    def effect(self):
        """Main entry point: check to see which mode/tab is selected, and act accordingly."""
        self.start_time = time.time()

        try:
            self.Secondary
        except AttributeError:
            self.Secondary = False

        self.text_out = '' # Text log for basic communication messages
        self.error_out = '' # Text log for significant errors

        self.plot_status.stats.reset() # Reset plot duration and distance statistics
        self.time_estimate = 0.0 # plot time estimate, s. Available to Python API

        self.doc_units = "in"

        if self.start_x is not None:
            self.f_curr_x = self.start_x
        else:
            self.f_curr_x = self.params.start_pos_x

        if self.start_y is not None:
            self.f_curr_y = self.start_y
        else:
            self.f_curr_y = self.params.start_pos_y

        self.pt_first = (self.f_curr_x, self.f_curr_y)

        self.node_target = int(0)
        self.layer_speed_pendown = -1
        self.plot_status.copies_to_plot = 1

        # New values to write to file:
        self.svg_layer = int(0)
        self.svg_node_count = int(0)
        self.svg_data_read = False
        self.svg_data_written = False
        self.svg_last_path = int(0)
        self.svg_last_path_nc = int(0)
        self.svg_last_known_pos_x = float(0.0)
        self.svg_last_known_pos_y = float(0.0)
        self.svg_paused_x = float(0.0)
        self.svg_paused_y = float(0.0)
        self.svg_rand_seed = int(1)
        self.svg_width = 0
        self.svg_height = 0
        self.rotate_page = False

        self.update_options()

        self.path_data_pu = []  # pen-up path data for preview layers
        self.path_data_pd = []  # pen-down path data for preview layers

        self.vel_data_plot = False
        self.vel_data_time = 0
        self.vel_chart1 = [] # Velocity chart, for preview of velocity vs time Motor 1
        self.vel_chart2 = []  # Velocity chart, for preview of velocity vs time Motor 2
        self.vel_data_chart_t = [] # Velocity chart, for preview of velocity vs time Total V

        self.options.mode = self.options.mode.strip("\"") # Input sanitization
        self.options.setup_type = self.options.setup_type.strip("\"")
        self.options.manual_cmd = self.options.manual_cmd.strip("\"")
        self.options.resume_type = self.options.resume_type.strip("\"")

        try:
            self.called_externally
        except AttributeError:
            self.called_externally = False

        if self.options.mode == "options":
            return
        if self.options.mode == "timing":
            return
        if self.options.mode == "version":
            # Return the version of _this python script_.
            self.user_message_fun(self.version_string)
            return
        if self.options.mode == "manual":
            if self.options.manual_cmd == "none":
                return  # No option selected. Do nothing and return no error.
            if self.options.manual_cmd == "strip_data":
                self.svg = self.document.getroot()
                for slug in ['WCB', 'MergeData', 'plotdata', 'eggbot']:
                    for node in self.svg.xpath('//svg:' + slug, namespaces=inkex.NSS):
                        self.svg.remove(node)
                self.user_message_fun(gettext.gettext(\
                    "All AxiDraw data has been removed from this SVG file."))
                return
            if self.options.manual_cmd == "list_names":
                self.name_list = ebb_serial.list_named_ebbs() # Variable available for python API
                if not self.name_list:
                    self.user_message_fun(gettext.gettext("No named AxiDraw units located.\n"))
                else:
                    self.user_message_fun(gettext.gettext("List of attached AxiDraw units:"))
                    for detected_ebb in self.name_list:
                        self.user_message_fun(detected_ebb)
                return

        if self.options.mode == "resume":
            # resume mode + resume_type -> either  res_plot or res_home modes.
            if self.options.resume_type == "home":
                self.options.mode = "res_home"
            else:
                self.options.mode = "res_plot"

        if self.options.mode == "setup":
            # setup mode -> either align, toggle, or cycle modes.
            self.options.mode = self.options.setup_type

        if self.options.digest > 1: # Generate digest only; do not run plot or preview
            self.options.preview = True # Disable serial communication; restrict certain functions

        if not self.options.preview:
            self.serial_connect()

        if self.options.mode == "sysinfo":
            versions.log_version_info(self.plot_status.port, self.params.check_updates,
                                      self.version_string, self.options.preview,
                                      self.user_message_fun, logger)

        if self.plot_status.port is None and not self.options.preview:
            return # unable to connect to axidraw

        self.svg = self.document.getroot()
        self.resume_data_needs_updating = False

        self.options.page_delay = max(self.options.page_delay, 0)

        if self.plot_status.progress.review(self.plot_status, self.options): # CLI progress bar
            if self.options.mode == "layers": # prepare for dry run
                self.svg_layer = self.options.layer
            else:
                self.svg_layer = -1  # Plotting all layers
            self.read_plotdata(self.svg) # Is this necessary? Possibly, for resume
            if self.options.mode == "res_plot":
                self.resume_plot_setup()
            self.plot_document() # "Dry run": Calculate plot time & node count for progress bar
            self.user_message_fun(
                self.plot_status.progress.restore(self.plot_status, self.options))

        self.read_plotdata(self.svg)
        if self.options.mode == "plot":
            self.plot_status.copies_to_plot = self.options.copies
            if self.plot_status.copies_to_plot == 0:
                self.plot_status.copies_to_plot = -1
                if self.options.preview:
                    # Special case: 0 (continuous copies), but in preview mode.
                    self.plot_status.copies_to_plot = 1
                    # In this case, revert back to single copy, since there is
                    # essentially no way to terminate.
            while self.plot_status.copies_to_plot != 0:
                self.resume_data_needs_updating = True
                # # New random seed for new plot; Changes every 10 ms:
                self.svg_rand_seed = int(time.time()*100)

                self.pathcount = 0
                self.svg_node_count = 0
                self.svg_last_path = 0
                self.svg_layer = -1  # indicate (to resume routine) that we are plotting all layers

                self.plot_status.delay_between_copies = False # Not delaying between copies
                self.plot_status.copies_to_plot -= 1
                self.plot_document()
                self.plot_status.delay_between_copies = True  # Currently delaying between copies

                if self.plot_status.copies_to_plot == 0 or self.plot_status.stopped:
                    continue # No delay after last copy, or if stopped.

                self.plot_status.progress.launch(self.plot_status, self.options, True,
                    1000 * self.options.page_delay)
                time_counter = 10 * self.options.page_delay
                while time_counter > 0:
                    time_counter -= 1
                    if self.plot_status.copies_to_plot != 0 and not self.plot_status.stopped:
                        # Delay if we're between copies, not after the last or paused.
                        self.plot_status.stats.page_delays += 100
                        if self.options.preview:
                            self.plot_status.stats.pt_estimate += 100
                        else:
                            time.sleep(0.100)  # Use short intervals to improve responsiveness
                            self.plot_status.progress.update_rel(100)
                            self.pause_res_check() # Detect button press while between plots
                            if self.plot_status.stopped:
                                self.plot_status.copies_to_plot = 0
                self.plot_status.progress.close()
        elif self.options.mode in ["res_home", "res_plot"]:
            self.resume_data_needs_updating = True
            self.resume_plot_setup()
            if self.plot_status.resume.resume_mode:
                self.plot_status.copies_to_plot = 0 # Flag for reporting plot time
                self.plot_document()
            elif self.options.mode == "res_home":
                if not self.svg_data_read:
                    logger.error(gettext.gettext("No resume data found; unable to return Home."))
                    return
                if not self.layer_found:
                    logger.error(gettext.gettext(\
                        "No in-progress plot data found; unable to return to Home position."))
                    return
                if (math.fabs(self.svg_last_known_x_old < 0.001) and
                        math.fabs(self.svg_last_known_y_old < 0.001)):
                    logger.error(gettext.gettext(\
                        "Unable to move to Home. (Is the AxiDraw already at Home?)"))
                    return
                self.plot_document()
                self.svg_node_count = self.svg_node_count_old # Save old values, to resume later.
                self.svg_last_path = self.svg_last_path_old
                self.svg_last_path_nc = self.svg_last_path_nc_old
                self.svg_paused_x = self.svg_paused_x_old
                self.svg_paused_y = self.svg_paused_y_old
                self.svg_layer = self.svg_layer_old
                self.svg_rand_seed = self.svg_rand_seed_old
            else:
                logger.error(gettext.gettext(\
                    "No in-progress plot data found in file; unable to resume."))

        elif self.options.mode == "layers":
            self.plot_status.copies_to_plot = self.options.copies
            if self.plot_status.copies_to_plot == 0: # Special case: Continuous copies selected
                self.plot_status.copies_to_plot = -1 # Flag for continuous copies
                if self.options.preview:    # However in preview mode, if continuous is
                    self.plot_status.copies_to_plot = 1 #  selected, then only run a single copy.
            while self.plot_status.copies_to_plot != 0:
                self.resume_data_needs_updating = True
                self.svg_rand_seed = int(time.time() * 100)  # New random seed for new plot
                self.svg_last_path = 0
                self.svg_node_count = 0
                self.pathcount = 0
                self.svg_layer = self.options.layer
                self.plot_status.delay_between_copies = False
                self.plot_status.copies_to_plot -= 1
                self.plot_document()
                self.plot_status.delay_between_copies = True # Currently delaying between copies
                self.plot_status.progress.launch(self.plot_status, self.options, True,
                    1000 * self.options.page_delay)
                time_counter = 10 * self.options.page_delay
                while time_counter > 0:
                    time_counter -= 1
                    if self.plot_status.copies_to_plot != 0 and not self.plot_status.stopped:
                        # Delay if we're between copies, not after the last or paused.
                        if self.options.preview:
                            self.plot_status.stats.pt_estimate += 100
                            self.plot_status.stats.page_delays += 100
                        else:
                            time.sleep(0.100)  # Use short intervals to improve responsiveness
                            self.plot_status.stats.page_delays += 100
                            self.plot_status.progress.update_rel(100)
                            self.pause_res_check() # Detect button press while between plots
                            if self.plot_status.stopped:
                                self.plot_status.copies_to_plot = 0
                self.plot_status.progress.close()
        elif self.options.mode in ('align', 'toggle', 'cycle'):
            self.setup_command()

        elif self.options.mode == "manual":
            self.manual_command() # Handle manual commands that use both power and usb.

        if self.resume_data_needs_updating:
            self.update_plotdata()
        if self.plot_status.port is not None:
            ebb_motion.doTimedPause(self.plot_status.port, 10, False) # Final timed motion command
            if self.options.port is None:  # Do not close serial port if it was opened externally.
                self.disconnect()

        if not self.called_externally: # Print optional time reports
            for warning_message in self.warnings.return_text_list():
                self.user_message_fun(warning_message)


    def resume_plot_setup(self):
        """ Initialization for resuming plots """
        self.layer_found = False # No layer number found in stored plot data
        if -1 <= self.svg_layer_old < 1001:
            self.layer_found = True
        if self.layer_found:
            if self.svg_node_count_old > 0:
                # Preset last path counts, handles case where the plot is paused again
                #    before completing any full paths
                self.svg_last_path = self.svg_last_path_old
                self.svg_last_path_nc = self.svg_last_path_nc_old
                self.svg_last_known_pos_x = self.svg_last_known_x_old
                self.svg_last_known_pos_y = self.svg_last_known_y_old

                self.node_target = self.svg_node_count_old
                self.svg_layer = self.svg_layer_old
                self.pen.servo_setup_wrapper(self.options, self.params, self.plot_status)
                v_time = self.pen.pen_raise(self.options, self.params, self.plot_status)
                self.v_chart_rest(v_time)
                self.enable_motors()  # Set plotting resolution
                if self.options.mode == "res_plot":
                    self.plot_status.resume.resume_mode = True

                self.f_curr_x = self.svg_last_known_x_old + self.pt_first[0]
                self.f_curr_y = self.svg_last_known_y_old + self.pt_first[1]

                self.svg_rand_seed = self.svg_rand_seed_old  # Use old random seed
                logger.debug('Entering resume mode at layer:  ' + str(self.svg_layer))

    def read_plotdata(self, svg_to_check):
        """ Read plot progress data, stored in a custom "plotdata" XML element """
        self.svg_data_read = False
        data_node = None
        nodes = svg_to_check.xpath("//*[self::svg:plotdata|self::plotdata]", namespaces=inkex.NSS)
        if nodes:
            data_node = nodes[0]
        if data_node is not None:
            try: # Core data required for resuming plots
                self.svg_layer_old = int(data_node.get('layer'))
                self.svg_node_count_old = int(data_node.get('node'))
                self.svg_last_path_old = int(data_node.get('last_path'))
                self.svg_last_path_nc_old = int(data_node.get('node_after_path'))
                self.svg_last_known_x_old = float(data_node.get('last_known_x'))
                self.svg_last_known_y_old = float(data_node.get('last_known_y'))
                self.svg_paused_x_old = float(data_node.get('paused_x'))
                self.svg_paused_y_old = float(data_node.get('paused_y'))
                self.svg_data_read = True
                self.svg_application_old = data_node.get('application')
                self.svg_plob_version = data_node.get('plob_version')
            except TypeError: # An error leaves svg_data_read as False.
                self.svg.remove(data_node) # Remove data node
            try: # Optional attributes:
                self.svg_row_old = int(data_node.get('row'))
            except TypeError:
                pass  # Leave as default if not found
            try: # Optional attributes:
                self.svg_rand_seed_old = int(float(data_node.get('randseed')))
            except TypeError:
                pass  # Leave as default if not found

    def update_plotdata(self):
        """ Write plot progress data, stored in a custom "plotdata" XML element """
        if not self.svg_data_written:
            for node in self.svg.xpath("//*[self::svg:plotdata|self::plotdata]",\
                namespaces=inkex.NSS):
                node_parent = node.getparent()
                node_parent.remove(node)
            data_node = etree.SubElement(self.svg, 'plotdata')
            data_node.set('application', "axidraw")  # Name of this program
            data_node.set('model', str(self.options.model))
            if self.options.digest: # i.e., if self.options.digest > 0
                data_node.set('plob_version', str(path_objects.PLOB_VERSION))
            elif self.svg_plob_version:
                data_node.set('plob_version', str(self.svg_plob_version))
            data_node.set('layer', str(self.svg_layer))
            data_node.set('node', str(self.svg_node_count))
            data_node.set('last_path', str(self.svg_last_path))
            data_node.set('node_after_path', str(self.svg_last_path_nc))
            data_node.set('last_known_x', str(self.svg_last_known_pos_x))
            data_node.set('last_known_y', str(self.svg_last_known_pos_y))
            data_node.set('paused_x', str(self.svg_paused_x))
            data_node.set('paused_y', str(self.svg_paused_y))
            data_node.set('randseed', str(self.svg_rand_seed))
            data_node.set('row', str(self.svg_row_old))
            data_node.set('id', str(int(time.time())))
            self.svg_data_written = True

    def setup_command(self):
        """ Execute commands from the setup modes """

        if self.options.preview:
            self.user_message_fun('Command unavailable while in preview mode.')
            return

        if self.plot_status.port is None:
            return

        self.query_ebb_voltage()
        self.pen.servo_setup_wrapper(self.options, self.params, self.plot_status)

        if self.options.mode == "align":
            self.pen.pen_raise(self.options, self.params, self.plot_status)
            ebb_motion.sendDisableMotors(self.plot_status.port, False)
        elif self.options.mode == "toggle":
            self.pen.toggle(self.options, self.params, self.plot_status)
        elif self.options.mode == "cycle":
            self.pen.cycle(self.options, self.params, self.plot_status)

    def manual_command(self):
        """ Execute commands in the "manual" mode/tab """

        # First: Commands that require serial but not power:
        if self.options.preview:
            self.user_message_fun('Command unavailable while in preview mode.')
            return

        if self.plot_status.port is None:
            return

        if self.options.manual_cmd == "fw_version":
            self.user_message_fun(self.plot_status.fw_version)
            return

        if self.options.manual_cmd == "bootload":
            success = ebb_serial.bootload(self.plot_status.port)
            if success:
                self.user_message_fun(
                    gettext.gettext("Entering bootloader mode for firmware programming.\n" +
                                    "To resume normal operation, you will need to first\n" +
                                    "disconnect the AxiDraw from both USB and power."))
                self.disconnect() # Disconnect from AxiDraw; end serial session
            else:
                logger.error('Failed while trying to enter bootloader.')
            return

        if self.options.manual_cmd == "read_name":
            name_string = ebb_serial.query_nickname(self.plot_status.port)
            if name_string is None:
                logger.error(gettext.gettext("Error; unable to read nickname.\n"))
            else:
                self.user_message_fun(name_string)
            return

        if (self.options.manual_cmd).startswith("write_name"):
            temp_string = self.options.manual_cmd
            temp_string = temp_string.split("write_name", 1)[1] # Get part after "write_name"
            temp_string = temp_string[:16] # Only use first 16 characters in name
            if not temp_string:
                temp_string = "" # Use empty string to clear nickname.
            if ebb_serial.min_version(self.plot_status.port, "2.5.5"):
                renamed = ebb_serial.write_nickname(self.plot_status.port, temp_string)
                if renamed is True:
                    self.user_message_fun('Nickname written. Rebooting EBB.')
                else:
                    logger.error('Error encountered while writing nickname.')
                ebb_serial.reboot(self.plot_status.port)    # Reboot required after writing nickname
                self.disconnect() # Disconnect from AxiDraw; end serial session
            else:
                logger.error("This function requires a newer firmware version. See: axidraw.com/fw")
            return

        # Next: Commands that require both power and serial connectivity:
        self.query_ebb_voltage()
        # Query if button pressed, to clear the result:
        ebb_motion.QueryPRGButton(self.plot_status.port, False)
        if self.options.manual_cmd == "raise_pen":
            self.pen.servo_setup_wrapper(self.options, self.params, self.plot_status)
            self.pen.pen_raise(self.options, self.params, self.plot_status)
        elif self.options.manual_cmd == "lower_pen":
            self.pen.servo_setup_wrapper(self.options, self.params, self.plot_status)
            self.pen.pen_lower(self.options, self.params, self.plot_status)
        elif self.options.manual_cmd == "enable_xy":
            self.enable_motors()
        elif self.options.manual_cmd == "disable_xy":
            ebb_motion.sendDisableMotors(self.plot_status.port, False)
        else:  # walk motors or move home cases:
            self.pen.servo_setup_wrapper(self.options, self.params, self.plot_status)
            self.enable_motors()  # Set plotting resolution
            if self.options.manual_cmd == "walk_home":
                if ebb_serial.min_version(self.plot_status.port, "2.6.2"):
                    a_pos, b_pos = ebb_motion.query_steps(self.plot_status.port, False)
                    n_delta_x = -(a_pos + b_pos) / (4 * self.params.native_res_factor)
                    n_delta_y = -(a_pos - b_pos) / (4 * self.params.native_res_factor)
                    if self.options.resolution == 2:  # Low-resolution mode
                        n_delta_x *= 2
                        n_delta_y *= 2
                else:
                    logger.error("This function requires newer firmware. Update at: axidraw.com/fw")
                    return
            elif self.options.manual_cmd == "walk_y":
                n_delta_x = 0
                n_delta_y = self.options.walk_dist
            elif self.options.manual_cmd == "walk_x":
                n_delta_y = 0
                n_delta_x = self.options.walk_dist
            elif self.options.manual_cmd == "walk_mmy":
                n_delta_x = 0
                n_delta_y = self.options.walk_dist / 25.4
            elif self.options.manual_cmd == "walk_mmx":
                n_delta_y = 0
                n_delta_x = self.options.walk_dist / 25.4
            else:
                return

            self.f_curr_x = self.svg_last_known_x_old + self.pt_first[0]
            self.f_curr_y = self.svg_last_known_y_old + self.pt_first[1]
            f_x = self.f_curr_x + n_delta_x # Note: Walks are relative, not absolute!
            f_y = self.f_curr_y + n_delta_y # New position is not saved; use with care.
            self.plot_seg_with_v(f_x, f_y, 0, 0, ignore_limits=True)


    def v_chart_rest(self, v_time):
        """
        Update velocity charts and plot time estimate with a zero-velocity segment for
        given time duration; typically used after raising or lowering the pen.
        """
        if not self.options.preview:
            return
        self.update_v_charts(0, 0, 0)
        self.vel_data_time += v_time
        self.update_v_charts(0, 0, 0)
        self.plot_status.stats.pt_estimate += v_time


    def update_v_charts(self, v_1, v_2, v_total):
        """ Update velocity charts, using some appropriate scaling for X and Y display."""
        if not self.vel_data_plot:
            return
        temp_time = self.vel_data_time / 1000.0
        scale_factor = 10.0 / self.options.resolution
        self.vel_chart1.append(" {0:0.3f} {1:0.3f}".format(temp_time, 8.5 - v_1 / scale_factor))
        self.vel_chart2.append(" {0:0.3f} {1:0.3f}".format(temp_time, 8.5 - v_2 / scale_factor))
        self.vel_data_chart_t.append(\
            " {0:0.3f} {1:0.3f}".format(temp_time, 8.5 - v_total / scale_factor))


    def plot_document(self):
        """ Plot the actual SVG document, if so selected in the interface """
        if not self.get_doc_props():
            # Error: This document appears to have inappropriate (or missing) dimensions.
            self.user_message_fun(gettext.gettext('This document does not have valid dimensions.'))
            self.user_message_fun(gettext.gettext(
                'The page size should be in either millimeters (mm) or inches (in).\r\r'))
            self.user_message_fun(gettext.gettext(
                'Consider starting with the Letter landscape or '))
            self.user_message_fun(gettext.gettext('the A4 landscape template.\r\r'))
            self.user_message_fun(gettext.gettext('The page size may also be set in Inkscape,\r'))
            self.user_message_fun(gettext.gettext('using File > Document Properties.'))
            return

        if not self.options.preview:
            self.options.rendering = 0 # Only render previews if we are in preview mode.
            self.vel_data_plot = False
            if self.plot_status.port is None:
                return
            self.query_ebb_voltage()
            _unused = ebb_motion.QueryPRGButton(self.plot_status.port, False) # Initialize button

        self.plot_status.progress.launch(self.plot_status, self.options)

        if not hasattr(self, 'backup_original'):
            self.backup_original = copy.deepcopy(self.document)

        # Modifications to SVG -- including re-ordering and text substitution
        #   may be made at this point, and will not be preserved.

        v_b = self.svg.get('viewBox')
        if v_b:
            p_a_r = self.svg.get('preserveAspectRatio')
            s_x, s_y, o_x, o_y = plot_utils.vb_scale(v_b, p_a_r, self.svg_width, self.svg_height)
        else:
            s_x = 1.0 / float(plot_utils.PX_PER_INCH) # Handle case of no viewbox
            s_y = s_x
            o_x = 0.0
            o_y = 0.0

        # Initial transform of document is based on viewbox, if present:
        self.svg_transform = simpletransform.parseTransform(\
                'scale({0:.6E},{1:.6E}) translate({2:.6E},{3:.6E})'.format(s_x, s_y, o_x, o_y))

        valid_plob = False
        if self.svg_plob_version:
            logger.debug('Checking Plob')
            valid_plob = digest_svg.verify_plob(self.svg, self.options.model)
        if valid_plob:
            logger.debug('Valid plob found; skipping standard pre-processing.')
            digest = path_objects.DocDigest()
            digest.from_plob(self.svg)
        else: # Process the input SVG into a simplified, restricted-format DocDigest object:
            digester = digest_svg.DigestSVG() # Initialize class
            digest_params = [self.svg_width, self.svg_height, s_x, s_y, self.svg_layer,\
                self.params.curve_tolerance, self.params.segment_supersample_tolerance]

            digest = digester.process_svg(self.svg, self.warnings,
                digest_params, self.svg_transform,)

            """
            Possible future work: Perform hidden-line clipping at this point, based on object
                fills, clipping masks, and document and plotting bounds, via self.bounds
            """
            """
            Possible future work: Perform automatic hatch filling at this point, based on object
                fill colors and possibly other factors.
            """

            digest.flatten() # Flatten digest, readying it for optimizations and plotting

            if self.rotate_page: # Rotate digest
                digest.rotate(self.params.auto_rotate_ccw)

            """
            Clip digest at plot bounds
            """
            if self.rotate_page:
                doc_bounds = [self.svg_height + 1e-9, self.svg_width + 1e-9]
            else:
                doc_bounds = [self.svg_width + 1e-9, self.svg_height + 1e-9]
            out_of_bounds_flag = boundsclip.clip_at_bounds(digest, self.bounds, doc_bounds,\
                self.params.bounds_tolerance, self.params.clip_to_page)
            if out_of_bounds_flag:
                self.warnings.add_new('bounds')
            """
            Optimize digest
            """
            allow_reverse = self.options.reordering in [2, 3]

            if self.options.reordering < 3: # Set reordering to 4 to disable path joining
                plot_optimizations.connect_nearby_ends(digest, allow_reverse, self.params.min_gap)

            if self.options.random_start:
                plot_optimizations.randomize_start(digest, self.svg_rand_seed)

            if self.options.reordering in {1, 2, 3}:
                plot_optimizations.reorder(digest, allow_reverse)

        # If it is necessary to save as a Plob, that conversion can be made like so:
        # plob = digest.to_plob() # Unnecessary re-conversion for testing only
        # digest.from_plob(plob)  # Unnecessary re-conversion for testing only

        if self.options.digest > 1: # No plotting; generate digest only.
            self.document = copy.deepcopy(digest.to_plob())
            self.svg = self.document
            return

        try:  # wrap everything in a try so we can be sure to close the serial port
            self.pen.servo_setup_wrapper(self.options, self.params, self.plot_status)
            v_time = self.pen.pen_raise(self.options, self.params, self.plot_status)
            self.v_chart_rest(v_time)
            self.enable_motors()  # Set plotting resolution

            if self.options.mode in ('res_home', 'res_plot'):
                if self.plot_status.resume.resume_mode:
                    f_x = self.svg_paused_x_old + self.pt_first[0]
                    f_y = self.svg_paused_y_old + self.pt_first[1]
                    self.plot_status.resume.resume_mode = False # __Temporarily__ disable (!)
                    self.plot_seg_with_v(f_x, f_y, 0, 0) # pen-up move to starting point
                    self.plot_status.resume.resume_mode = True
                    self.node_count = 0 # Clear this _after_ move to first point.
                    self.pathcount = 0
                elif self.options.mode == "res_home":
                    f_x = self.pt_first[0]
                    f_y = self.pt_first[1]
                    self.plot_seg_with_v(f_x, f_y, 0, 0)
                    return
                else:
                    self.user_message_fun(gettext.gettext('Resume plot error; plot terminated'))
                    return # something has gone wrong; possibly an ill-timed button press?

            self.plot_doc_digest(digest) # Step through and plot contents of document digest

            v_time = self.pen.pen_raise(self.options, self.params, self.plot_status)
            self.v_chart_rest(v_time)

            if self.plot_status.stopped == 0 and self.pt_first: # Return Home after normal plot
                self.x_bounds_min = 0
                self.y_bounds_min = 0
                if self.end_x is not None:  # Option for different final XY position:
                    f_x = self.end_x
                else:
                    f_x = self.pt_first[0]
                if self.end_y is not None:
                    f_y = self.end_y
                else:
                    f_y = self.pt_first[1]

                self.node_count = self.node_target
                self.plot_seg_with_v(f_x, f_y, 0, 0)

            """
            Revert back to original SVG document, prior to adding preview layers.
             and prior to saving updated "plotdata" progress data in the file.
             No changes to the SVG document prior to this point will be saved.

            Doing so allows us to use routines that alter the SVG prior to this point,
             e.g., plot re-ordering for speed or font substitutions.
            """
            try:
                if self.options.digest:
                    self.document = copy.deepcopy(digest.to_plob())
                    self.svg = self.document
                    self.options.rendering = 0 # Turn off rendering
                else:
                    self.document = copy.deepcopy(self.backup_original)
                    self.svg = self.document.getroot()
            except AttributeError:
                self.document = copy.deepcopy(self.original_document)
                self.svg = self.document.getroot()

            if self.plot_status.stopped == 0: # If the plot has ended normally...
                if self.options.mode in ["plot", "layers", "res_home", "res_plot"]:
                    # Clear saved plot data from the SVG file,
                    self.svg_layer = -2
                    self.svg_node_count = 0
                    self.svg_last_path = 0
                    self.svg_last_path_nc = 0
                    self.svg_last_known_pos_x = 0
                    self.svg_last_known_pos_y = 0
                    self.svg_paused_x = 0
                    self.svg_paused_y = 0
                    self.svg_rand_seed = 0

            if self.options.preview:
                # Remove old preview layers, whenever preview mode is enabled
                for node in self.svg:
                    if node.tag == inkex.addNS('g', 'svg') or node.tag == 'g':
                        if node.get(inkex.addNS('groupmode', 'inkscape')) == 'layer':
                            layer_name = node.get(inkex.addNS('label', 'inkscape'))
                            if layer_name == '% Preview':
                                self.svg.remove(node)

            if self.options.rendering > 0:  # Render preview. Only possible when in preview mode.
                preview_transform = simpletransform.parseTransform(
                    'translate({2:.6E},{3:.6E}) scale({0:.6E},{1:.6E})'.format(
                    1.0/s_x, 1.0/s_y, -o_x, -o_y))
                path_attrs = { 'transform': simpletransform.formatTransform(preview_transform)}
                preview_layer = etree.Element(inkex.addNS('g', 'svg'),
                    path_attrs, nsmap=inkex.NSS)

                preview_sl_u = etree.SubElement(preview_layer, inkex.addNS('g', 'svg'))
                preview_sl_d = etree.SubElement(preview_layer, inkex.addNS('g', 'svg'))

                preview_layer.set(inkex.addNS('groupmode', 'inkscape'), 'layer')
                preview_layer.set(inkex.addNS('label', 'inkscape'), '% Preview')
                preview_sl_d.set(inkex.addNS('groupmode', 'inkscape'), 'layer')
                preview_sl_d.set(inkex.addNS('label', 'inkscape'), 'Pen-down movement')
                preview_sl_u.set(inkex.addNS('groupmode', 'inkscape'), 'layer')
                preview_sl_u.set(inkex.addNS('label', 'inkscape'), 'Pen-up movement')

                self.svg.append(preview_layer)

                # Preview stroke width: 1/1000 of page width or height, whichever is smaller
                if self.svg_width < self.svg_height:
                    width_du = self.svg_width / 1000.0
                else:
                    width_du = self.svg_height / 1000.0

                """
                Stroke-width is a css style element, and cannot accept scientific notation.

                Thus, in cases with large scaling (i.e., high values of 1/sx, 1/sy) resulting
                from the viewbox attribute of the SVG document, it may be necessary to use 
                a _very small_ stroke width, so that the stroke width displayed on the screen
                has a reasonable width after being displayed greatly magnified by the viewbox.

                Use log10(the number) to determine the scale, and thus the precision needed.
                """
                log_ten = math.log10(width_du)
                if log_ten > 0:  # For width_du > 1
                    width_string = "{0:.3f}".format(width_du)
                else:
                    prec = int(math.ceil(-log_ten) + 3)
                    width_string = "{0:.{1}f}".format(width_du, prec)

                p_style = {'stroke-width': width_string, 'fill': 'none',
                    'stroke-linejoin': 'round', 'stroke-linecap': 'round'}

                ns_prefix = "plot"
                if self.options.rendering > 1:
                    p_style.update({'stroke': self.params.preview_color_up})
                    path_attrs = {
                        'style': simplestyle.formatStyle(p_style),
                        'd': " ".join(self.path_data_pu),
                        inkex.addNS('desc', ns_prefix): "pen-up transit"}
                    etree.SubElement(preview_sl_u,
                                     inkex.addNS('path', 'svg '), path_attrs, nsmap=inkex.NSS)

                if self.options.rendering in (1, 3):
                    p_style.update({'stroke': self.params.preview_color_down})
                    path_attrs = {
                        'style': simplestyle.formatStyle(p_style),
                        'd': " ".join(self.path_data_pd),
                        inkex.addNS('desc', ns_prefix): "pen-down drawing"}
                    etree.SubElement(preview_sl_d,
                                     inkex.addNS('path', 'svg '), path_attrs, nsmap=inkex.NSS)

                if self.options.rendering > 0 and self.vel_data_plot: # Preview enabled w/ velocity
                    self.vel_chart1.insert(0, "M")
                    self.vel_chart2.insert(0, "M")
                    self.vel_data_chart_t.insert(0, "M")

                    p_style.update({'stroke': 'black'})
                    path_attrs = {
                        'style': simplestyle.formatStyle(p_style),
                        'd': " ".join(self.vel_data_chart_t),
                        inkex.addNS('desc', ns_prefix): "Total V"}
                    etree.SubElement(preview_layer,
                                     inkex.addNS('path', 'svg '), path_attrs, nsmap=inkex.NSS)

                    p_style.update({'stroke': 'red'})
                    path_attrs = {
                        'style': simplestyle.formatStyle(p_style),
                        'd': " ".join(self.vel_chart1),
                        inkex.addNS('desc', ns_prefix): "Motor 1 V"}
                    etree.SubElement(preview_layer,
                                     inkex.addNS('path', 'svg '), path_attrs, nsmap=inkex.NSS)

                    p_style.update({'stroke': 'green'})
                    path_attrs = {
                        'style': simplestyle.formatStyle(p_style),
                        'd': " ".join(self.vel_chart2),
                        inkex.addNS('desc', ns_prefix): "Motor 2 V"}
                    etree.SubElement(preview_layer,
                                     inkex.addNS('path', 'svg '), path_attrs, nsmap=inkex.NSS)

        finally: # In case of an exception and loss of the serial port...
            pass

        self.plot_status.progress.close()

        if self.plot_status.copies_to_plot == 0:  # Only calculate after plotting last copy

            if self.plot_status.progress.enable and self.plot_status.stopped == 0:
                self.user_message_fun("\nAxiCLI plot complete.\n") # If sequence ended normally.
            elapsed_time = time.time() - self.start_time
            self.time_elapsed = elapsed_time # Available for use by python API

            if not self.called_externally: # Print optional time reports
                self.plot_status.stats.report(self.options, self.user_message_fun, elapsed_time)
                self.pen.status.report(self.params, self.user_message_fun)

            if self.options.webhook and not self.options.preview:
                if self.options.webhook_url is not None:
                    payload = {'value1': str(digest.name),
                        'value2': str(text_utils.format_hms(elapsed_time)),
                        'value3': str(self.options.port),
                        }
                    try:
                        wh_result = requests.post(self.options.webhook_url, data=payload)
                        # self.user_message_fun("webhook results: " + str(wh_result))
                    except (RuntimeError, requests.exceptions.ConnectionError) as wh_err:
                        raise RuntimeError("An error occurred while posting webhook. " +
                                f"Are you connected to the internet? (Error: {wh_err})\n")

    def plot_doc_digest(self, digest):
        """
        Step through the document digest and plot each of the vertex lists.

        Takes a flattened path_objects.DocDigest object as input. All
        selection of elements to plot and their rendering, including
        transforms, needs to be handled before this routine.
        """

        if not digest:
            return

        for layer in digest.layers:

            self.pen.end_temp_height(self.options, self.params, self.plot_status)
            old_use_layer_speed = self.use_layer_speed  # A Boolean
            old_layer_speed_pendown = self.layer_speed_pendown  # Numeric value

            v_time = self.pen.pen_raise(self.options, self.params, self.plot_status)
            self.v_chart_rest(v_time)
            self.eval_layer_properties(layer.name) # Raise pen; compute with pen up.

            for path_item in layer.paths:
                if self.plot_status.stopped:
                    return

                # if we're in resume mode AND self.pathcount < self.svg_last_path, skip.
                # if we're in resume mode and self.pathcount = self.svg_last_path,
                # start here, and set self.node_count equal to self.svg_last_path_nc
                do_we_plot_this_path = True
                if self.plot_status.resume.resume_mode:
                    if self.pathcount < self.svg_last_path_old:
                        self.pathcount += 1
                        do_we_plot_this_path = False
                    elif self.pathcount == self.svg_last_path_old:
                        self.node_count = self.svg_last_path_nc_old
                if do_we_plot_this_path:
                    self.pathcount += 1
                    self.plot_polyline(path_item.subpaths[0])

            # Restore old layer status variables
            self.use_layer_speed = old_use_layer_speed

            if self.layer_speed_pendown != old_layer_speed_pendown:
                self.layer_speed_pendown = old_layer_speed_pendown
                self.enable_motors() # Set speed value variables for this layer.

            self.pen.end_temp_height(self.options, self.params, self.plot_status)


    def eval_layer_properties(self, str_layer_name):
        """
        Parse layer name for encoded commands.
        Syntax described at: https://wiki.evilmadscientist.com/AxiDraw_Layer_Control

        Parse characters following the layer number (if any) to see if there is
        a "+H" or "+S" escape code, that indicates that overrides the pen-down
        height or speed for a given layer. A "+D" indicates a given time delay.
        A leading "!" creates a programmatic pause.
        """

        temp_num_string = 'x'
        string_pos = 1
        current_layer_name = str(str_layer_name)
        current_layer_name.lstrip()  # Remove leading whitespace

        max_length = len(current_layer_name)
        if max_length > 0:
            if current_layer_name[0] == '!': # First character is "!"; insert programmatic pause

                # If in resume mode AND self.pathcount < self.svg_last_path, skip over this path.
                # If two or more forced pauses occur without any plotting between them, they
                # may be treated as a _single_ pause when resuming.

                if self.plot_status.resume.resume_mode:
                    if self.pathcount < self.svg_last_path_old:  # Fully plotted; skip.
                        # This pause was *already executed*, and we are resuming past it. Skip.
                        self.pathcount += 1
                else:
                    self.pathcount += 1  # Pause counts as a "path node" for pause/resume

                    # Record this as though it were a completed path:
                    self.svg_last_path = self.pathcount # The number of the last path completed
                    self.svg_last_path_nc = self.node_count # Node count after last path completed

                    if self.plot_status.stopped == 0: # If not already stopped
                        self.plot_status.stopped = -1 # Set flag for programmatic pause
                    self.pause_res_check()  # Carry out the pause, or resume if required.

                current_layer_name = current_layer_name[1:] # Remove leading '!'
                max_length -= 1
            while string_pos <= max_length:
                layer_name_fragment = current_layer_name[:string_pos]
                if layer_name_fragment.isdigit():
                    temp_num_string = current_layer_name[:string_pos] # Find longest numeric string
                    string_pos += 1
                else:
                    break

        old_speed = self.layer_speed_pendown

        self.use_layer_speed = False
        self.layer_speed_pendown = -1

        # Check to see if there is additional information coded in the layer name.
        if string_pos > 0:
            string_pos -= 1

        if max_length > string_pos + 2:
            while string_pos <= max_length:
                key = current_layer_name[string_pos:string_pos + 2].lower()
                if key in ('+h', '+s', '+d'):
                    param_start = string_pos + 2
                    string_pos += 3
                    temp_num_string = 'x'
                    if max_length > 0:
                        while string_pos <= max_length:
                            if str.isdigit(current_layer_name[param_start:string_pos]):
                                temp_num_string = current_layer_name[param_start:string_pos]
                                string_pos += 1
                            else:
                                break
                    if str.isdigit(temp_num_string):
                        parameter_int = int(float(temp_num_string))
                        if key == "+d":
                            if parameter_int > 0: # Delay time, ms
                                time_remaining = float(parameter_int) / 1000.0
                                while time_remaining > 0:
                                    if time_remaining < 0.15: # If less than 150 ms left to delay,
                                        time.sleep(time_remaining) #  then do it all at once.
                                        time_remaining = 0
                                        self.pause_res_check() # Was button pressed while delaying?
                                    else:
                                        time.sleep(0.1) # Use short intervals for responsiveness.
                                        time_remaining -= 0.1
                                        self.pause_res_check() # Was button pressed while delaying?
                        if key == "+h":
                            if 0 <= parameter_int <= 100:
                                self.pen.set_temp_height(self.options, self.params,
                                    parameter_int, self.plot_status)
                                # New height will be used when we next lower the pen.
                        if key == "+s":
                            if 0 < parameter_int <= 110:
                                self.use_layer_speed = True
                                self.layer_speed_pendown = parameter_int
                    string_pos = param_start + len(temp_num_string)
                else:
                    break  # exit loop.
        if self.layer_speed_pendown != old_speed:
            self.enable_motors()  # Set speed value variables for this layer.


    def plot_polyline(self, vertex_list):
        """
        Plot a polyline object; a single pen-down XY movement.
        - No transformations, no curves, no neat clipping at document bounds;
            those are all performed _before_ we get to this point.
        - Truncate motion, brute-force, at physical travel bounds, in case
            previous limits have failed or been overridden. No guarantee
            of graceful clipping, and no warning message will result.
        """

        # logger.debug('plot_polyline()\nPolyline vertex_list: ' + str(vertex_list))
        if self.plot_status.stopped:
            logger.debug('Polyline: self.plot_status.stopped.')
            return
        if not vertex_list:
            logger.debug('No vertex list to plot. Returning.')
            return
        if len(vertex_list) < 2:
            logger.debug('No full segments in vertex list. Returning.')
            return

        # Raise pen for travel to first vertex:
        # TODO: Remove this once motion.py is up and running
        v_time = self.pen.pen_raise(self.options, self.params, self.plot_status)
        self.v_chart_rest(v_time)

        for vertex in vertex_list:
            vertex[0], t_x = plot_utils.checkLimitsTol(vertex[0], 0, self.x_max_phy, 1e-9)
            vertex[1], t_y = plot_utils.checkLimitsTol(vertex[1], 0, self.y_max_phy, 1e-9)
            if t_x or t_y:
                logger.debug('Travel truncated to bounds at plot_polyline.')

        # Pen up straight move, zero velocity at endpoints, to first vertex location
        self.plot_seg_with_v(vertex_list[0][0], vertex_list[0][1], 0, 0)

         # **** Main programmatic pen lowering while plotting ****
        v_time = self.pen.pen_lower(self.options, self.params, self.plot_status)
        self.v_chart_rest(v_time)

        self.plan_trajectory(vertex_list)

        if self.plot_status.stopped == 0: # Populate our "index" for resuming plots quickly:
            self.svg_last_path = self.pathcount # The number of the last path completed
            self.svg_last_path_nc = self.node_count # Node count after the last path completed.


    def plan_trajectory(self, input_path):
        """
        Plan the trajectory for a full path, accounting for linear acceleration.
        Inputs: Ordered (x,y) pairs to cover.
        Output: A list of segments to plot, of the form (Xfinal, Yfinal, v_initial, v_final)
        [Aside: We may eventually migrate to the form (Xfinal, Yfinal, Vix, Viy, Vfx,Vfy)]

        Important note: This routine uses *inch* units (inches of distance, velocities of
        in/s, etc.), and works in the basis of the XY axes, not the native axes of the motors.
        """

        spew_trajectory_debug_data = self.spew_debugdata  # False or self.spew_debugdata

        traj_logger = logging.getLogger('.'.join([__name__, 'trajectory']))
        if spew_trajectory_debug_data:
            traj_logger.setLevel(logging.DEBUG) # by default level is INFO

        traj_logger.debug('\nplan_trajectory()\n')

        if self.plot_status.stopped:
            return
        if self.f_curr_x is None:
            return

        if len(input_path) < 2: # Invalid path segment
            return

        # Handle simple segments (lines) that do not require any complex planning:
        if len(input_path) < 3:
            traj_logger.debug('Drawing straight line, not a curve.')  # "SHORTPATH ESCAPE"
            traj_logger.debug('plot_seg_with_v({}, {}, {}, {})'.format(
                input_path[1][0], input_path[1][1], 0, 0))
            # Get X & Y Destination coordinates from last element, input_path[1]:
            self.plot_seg_with_v(input_path[1][0], input_path[1][1], 0, 0)
            return

        # For other trajectories, we need to go deeper.
        traj_length = len(input_path)

        traj_logger.debug('Input path to plan_trajectory: ')
        if traj_logger.isEnabledFor(logging.DEBUG):
            for xy in input_path:
                traj_logger.debug('x: {0:1.3f},  y: {1:1.3f}'.format(xy[0], xy[1]))
                traj_logger.debug('\ntraj_length: ' + str(traj_length))

        speed_limit = self.speed_pendown  # Maximum travel rate (in/s), in XY plane.
        if self.pen.status.pen_up:
            speed_limit = self.speed_penup  # For pen-up manual moves

        traj_logger.debug('\nspeed_limit (plan_trajectory) ' + str(speed_limit) + ' in/s')

        traj_dists = array('f')  # float, Segment length (distance) when arriving at the junction
        traj_vels = array('f')  # float, Velocity (_speed_, really) when arriving at the junction

        traj_vectors = []  # Array that will hold normalized unit vectors along each segment
        trimmed_path = []  # Array that will hold usable segments of input_path

        traj_dists.append(0.0)  # First value, at time t = 0
        traj_vels.append(0.0)  # First value, at time t = 0

        if self.options.resolution == 1:  # High-resolution mode
            min_dist = self.params.max_step_dist_hr # Skip segments likely to be < one step
        else:
            min_dist = self.params.max_step_dist_lr # Skip segments likely to be < one step

        last_index = 0
        for i in range(1, traj_length):
            # Construct arrays of position and distances, skipping near-zero length segments.

            # Distance per segment:
            tmp_dist_x = input_path[i][0] - input_path[last_index][0]
            tmp_dist_y = input_path[i][1] - input_path[last_index][1]

            tmp_dist = plot_utils.distance(tmp_dist_x, tmp_dist_y)

            if tmp_dist >= min_dist:
                traj_dists.append(tmp_dist)
                # Normalized unit vectors for computing cosine factor
                traj_vectors.append([tmp_dist_x / tmp_dist, tmp_dist_y / tmp_dist])
                tmp_x = input_path[i][0]
                tmp_y = input_path[i][1]
                trimmed_path.append([tmp_x, tmp_y])  # Selected, usable portions of input_path.

                traj_logger.debug('\nSegment: input_path[{0:1.0f}] -> input_path[{1:1.0f}]'.format
                    (last_index, i))
                traj_logger.debug('Dest: x: {0:1.3f},  y: {1:1.3f}. Distance: {2:1.3f}'.format(
                    tmp_x, tmp_y, tmp_dist))

                last_index = i
            else:
                traj_logger.debug('\nSegment: input_path[{0:1.0f}] -> input_path[{1:1.0f}]' +
                    ' is zero (or near zero); skipping!'.format(last_index, i))
                traj_logger.debug('  x: {0:1.3f},  y: {1:1.3f}, distance: {2:1.3f}'.format(
                    input_path[i][0], input_path[i][1], tmp_dist))

        traj_length = len(traj_dists)

        # Handle zero-segment plot:
        if traj_length < 2:
            traj_logger.debug('\nSkipped a path element without well-defined segments.')
            return

        # Remove zero-length elements and plot the element if it is just a line
        if traj_length < 3:
            traj_logger.debug('\nDrawing straight line, not a curve.')
            self.plot_seg_with_v(trimmed_path[0][0], trimmed_path[0][1], 0, 0)
            return

        traj_logger.debug('\nAfter removing any zero-length segments, we are left with: ')
        traj_logger.debug('traj_dists[0]: {0:1.3f}'.format(traj_dists[0]))
        if traj_logger.isEnabledFor(logging.DEBUG):
            for i in range(0, len(trimmed_path)):
                traj_logger.debug('i: {0:1.0f}, x: {1:1.3f}, y: {2:1.3f}, distance: ' +
                    '{3:1.3f}'.format(i, trimmed_path[i][0], trimmed_path[i][1], traj_dists[i + 1]))
                traj_logger.debug('  And... traj_dists[i+1]: {0:1.3f}'.format(traj_dists[i + 1]))

        # Acceleration/deceleration rates:
        if self.pen.status.pen_up:
            accel_rate = self.params.accel_rate_pu * self.options.accel / 100.0
        else:
            accel_rate = self.params.accel_rate * self.options.accel / 100.0

        # Maximum acceleration time: Time needed to accelerate from full stop to maximum speed:
        # v = a * t, so t_max = vMax / a
        t_max = speed_limit / accel_rate

        # Distance that is required to reach full speed, from zero speed:  x = 1/2 a t^2
        accel_dist = 0.5 * accel_rate * t_max * t_max

        traj_logger.debug('\nspeed_limit: {0:1.3f}'.format(speed_limit))
        traj_logger.debug('t_max: {0:1.3f}'.format(t_max))
        traj_logger.debug('accel_rate: {0:1.3f}'.format(accel_rate))
        traj_logger.debug('accel_dist: {0:1.3f}'.format(accel_dist))
        cosine_print_array = array('f')

        """
        Now, step through every vertex in the trajectory, and calculate what the speed
        should be when arriving at that vertex.

        In order to do so, we need to understand how the trajectory will evolve in terms
        of position and velocity for a certain amount of time in the future, past that vertex.
        The most extreme cases of this is when we are traveling at
        full speed initially, and must come to a complete stop.
            (This is actually more sudden than if we must reverse course-- that must also
            go through zero velocity at the same rate of deceleration, and a full reversal
            that does not occur at the path end might be able to have a
            nonzero velocity at the endpoint.)

        Thus, we look ahead from each vertex until one of the following occurs:
            (1) We have looked ahead by at least t_max, or
            (2) We reach the end of the path.

        The data that we have to start out with is this:
            - The position and velocity at the previous vertex
            - The position at the current vertex
            - The position at subsequent vertices
            - The velocity at the final vertex (zero)

        To determine the correct velocity at each vertex, we will apply the following rules:

        (A) For the first point, V(i = 0) = 0.

        (B) For the last point point, V = 0 as well.

        (C) If the length of the segment is greater than the distance
        required to reach full speed, then the vertex velocity may be as
        high as the maximum speed.

        Note that we must actually check not the total *speed* but the acceleration
        along the two native motor axes.

        (D) If not; if the length of the segment is less than the total distance
        required to get to full speed, then the velocity at that vertex
        is limited by to the value that can be reached from the initial
        starting velocity, in the distance given.

        (E) The maximum velocity through the junction is also limited by the
        turn itself-- if continuing straight, then we do not need to slow down
        as much as if we were fully reversing course.
        We will model each corner as a short curve that we can accelerate around.

        (F) To calculate the velocity through each turn, we must _look ahead_ to
        the subsequent (i+1) vertex, and determine what velocity
        is appropriate when we arrive at the next point.

        Because future points may be close together-- the subsequent vertex could
        occur just before the path end -- we actually must look ahead past the
        subsequent (i + 1) vertex, all the way up to the limits that we have described
        (e.g., t_max) to understand the subsequent behavior. Once we have that effective
        endpoint, we can work backwards, ensuring that we will be able to get to the
        final speed/position that we require.

        A less complete (but far simpler) procedure is to first complete the trajectory
        description, and then -- only once the trajectory is complete -- go back through,
        but backwards, and ensure that we can actually decelerate to each velocity.

        (G) The minimum velocity through a junction may be set to a constant.
        There is often some (very slow) speed -- perhaps a few percent of the maximum speed
        at which there are little or no resonances. Even when the path must directly reverse
        itself, we can usually travel at a non-zero speed. This, of course, presumes that we
        still have a solution for getting to the endpoint at zero speed.
        """

        delta = self.params.cornering / 5000  # Corner rounding/tolerance factor.

        for i in range(1, traj_length - 1):
            dcurrent = traj_dists[i]  # Length of the segment leading up to this vertex

            v_prev_exit = traj_vels[i - 1]  # Velocity when leaving previous vertex

            """
            Velocity at vertex: Part I

            Check to see what our plausible maximum speeds are, from
            acceleration only, without concern about cornering, nor deceleration.
            """

            if dcurrent > accel_dist:
                # There _is_ enough distance in the segment for us to either
                # accelerate to maximum speed or come to a full stop before this vertex.
                vcurrent_max = speed_limit
                traj_logger.debug('Speed Limit on vel : ' + str(i))
            else:
                # There is _not necessarily_ enough distance in the segment for us to either
                # accelerate to maximum speed or come to a full stop before this vertex.
                # Calculate how much we *can* swing the velocity by:

                vcurrent_max = plot_utils.vFinal_Vi_A_Dx(v_prev_exit, accel_rate, dcurrent)
                if vcurrent_max > speed_limit:
                    vcurrent_max = speed_limit

                traj_logger.debug('traj_vels I: {0:1.3f}'.format(vcurrent_max))

            """
            Velocity at vertex: Part II 
            
            Assuming that we have the same velocity when we enter and
            leave a corner, our acceleration limit provides a velocity
            that depends upon the angle between input and output directions.
            
            The cornering algorithm models the corner as a slightly smoothed corner,
            to estimate the angular acceleration that we encounter:
            https://onehossshay.wordpress.com/2011/09/24/improving_grbl_cornering_algorithm/
            
            The dot product of the unit vectors is equal to the cosine of the angle between the
            two unit vectors, giving the deflection between the incoming and outgoing angles. 
            Note that this angle is (pi - theta), in the convention of that article, giving us
            a sign inversion. [cos(pi - theta) = - cos(theta)]
            """
            cosine_factor = - plot_utils.dotProductXY(traj_vectors[i - 1], traj_vectors[i])

            root_factor = math.sqrt((1 - cosine_factor) / 2)
            denominator = 1 - root_factor
            if denominator > 0.0001:
                rfactor = (delta * root_factor) / denominator
            else:
                rfactor = 100000
            vjunction_max = math.sqrt(accel_rate * rfactor)

            vcurrent_max = min(vcurrent_max, vjunction_max)

            traj_vels.append(vcurrent_max)  # "Forward-going" speed limit at this vertex.
        traj_vels.append(0.0)  # Add zero velocity, for final vertex.

        if traj_logger.isEnabledFor(logging.DEBUG):
            traj_logger.debug(' ')
            for dist in cosine_print_array:
                traj_logger.debug('Cosine Factor: {0:1.3f}'.format(dist))
            traj_logger.debug(' ')

            for dist in traj_vels:
                traj_logger.debug('traj_vels II: {0:1.3f}'.format(dist))
            traj_logger.debug(' ')

        """
        Velocity at vertex: Part III

        We have, thus far, ensured that we could reach the desired velocities, going forward, but
        have also assumed an effectively infinite deceleration rate.

        We now go through the completed array in reverse, limiting velocities to ensure that we 
        can properly decelerate in the given distances.
        """

        for j in range(1, traj_length):
            i = traj_length - j  # Range: From (traj_length - 1) down to 1.

            v_final = traj_vels[i]
            v_initial = traj_vels[i - 1]
            seg_length = traj_dists[i]

            if v_initial > v_final and seg_length > 0:
                v_init_max = plot_utils.vInitial_VF_A_Dx(v_final, -accel_rate, seg_length)
                traj_logger.debug(
                    f'VInit Calc: (v_final = {v_final:1.3f}, accel_rate = {accel_rate:1.3f},' +
                    f' seg_length = {seg_length:1.3f}) ')
                if v_init_max < v_initial:
                    v_initial = v_init_max
                traj_vels[i - 1] = v_initial

        if traj_logger.isEnabledFor(logging.DEBUG):
            for dist in traj_vels:
                traj_logger.debug('traj_vels III: {0:1.3f}'.format(dist))
            traj_logger.debug(' ')

            # traj_logger.debug( 'List results for this input path:')
            # for i in range(0, traj_length-1):
            #     traj_logger.debug( 'i: %1.0f' %(i))
            #     traj_logger.debug( 'x: %1.3f, y: %1.3f' %(trimmed_path[i][0],trimmed_path[i][1]))
            #     traj_logger.debug( 'distance: %1.3f' %(traj_dists[i+1]))
            #     traj_logger.debug( 'traj_vels[i]: %1.3f' %(traj_vels[i]))
            #     traj_logger.debug( 'traj_vels[i+1]: %1.3f\n' %(traj_vels[i+1]))

        for i in range(0, traj_length - 1):
            self.plot_seg_with_v(trimmed_path[i][0], trimmed_path[i][1],
                traj_vels[i], traj_vels[i + 1])

    def plot_seg_with_v(self, x_dest, y_dest, v_i, v_f, ignore_limits=False):
        """
        Plot a straight line segment with given initial and final velocity.

        Controls the serial port to command the machine to draw
        a straight line segment, with basic acceleration support.

        Inputs:     Destination (x,y)
                    Initial velocity
                    Final velocity
                    ignore_limits (boolean): Used in manual walks to disregard travel limits

        Method: Divide the segment up into smaller segments, each
        of which has constant velocity.
        Send commands out the com port as a set of short line segments
        (dx, dy) with specified durations (in ms) of how long each segment
        takes to draw.the segments take to draw.
        Uses linear ("trapezoid") acceleration and deceleration strategy.

        Inputs are expected be in units of inches (for distance)
            or inches per second (for velocity).

        Input: A list of segments to plot, of the form (Xfinal, Yfinal, Vinitial, Vfinal)

        Input parameters are in distances of inches and velocities of inches per second.

        Within this routine, we convert from inches into motor steps.

        Note: Native motor axes are Motor 1, Motor 2:
            motor_dist1 = ( xDist + yDist ) # Distance for motor to move, Axis 1
            motor_dist2 = ( xDist - yDist ) # Distance for motor to move, Axis 2

        We will only discuss motor steps, and resolution, within the context of native axes.
        """

        self.pause_res_check()

        spew_segment_debug_data = self.spew_debugdata # Set true to display always

        seg_logger = logging.getLogger('.'.join([__name__, 'segment']))
        if spew_segment_debug_data:
            seg_logger.setLevel(logging.DEBUG) # by default level is INFO

        seg_logger.debug('\nplot_seg_with_v({0}, {1}, {2}, {3})'.format(x_dest, y_dest, v_i, v_f))
        if self.plot_status.resume.resume_mode or self.plot_status.stopped:
            spew_text = '\nSkipping '
        else:
            spew_text = '\nExecuting '
        spew_text += 'plot_seg_with_v() function\n'
        if self.pen.status.pen_up:
            spew_text += '  Pen-up transit'
        else:
            spew_text += '  Pen-down move'
        spew_text += ' from (x = {0:1.3f}, y = {1:1.3f})'.format(self.f_curr_x, self.f_curr_y)
        spew_text += ' to (x = {0:1.3f}, y = {1:1.3f})\n'.format(x_dest, y_dest)
        spew_text += '    w/ v_i = {0:1.2f}, v_f = {1:1.2f} '.format(v_i, v_f)
        seg_logger.debug(spew_text)
        if self.plot_status.resume.resume_mode:
            seg_logger.debug(' -> NOTE: ResumeMode is active')
        if self.plot_status.stopped:
            seg_logger.debug(' -> NOTE: Plot is in a Stopped state.')

        constant_vel_mode = False
        if self.options.const_speed and not self.pen.status.pen_up:
            constant_vel_mode = True

        if self.plot_status.stopped:
            self.plot_status.copies_to_plot = 0
            return
        if self.f_curr_x is None:
            return

        if not ignore_limits:  # check page size limits:
            tolerance = self.params.bounds_tolerance # Truncate up to 1 step w/o error.
            x_dest, x_bounded = plot_utils.checkLimitsTol(x_dest,
                self.x_bounds_min, self.x_bounds_max, tolerance)
            y_dest, y_bounded = plot_utils.checkLimitsTol(y_dest,
                self.y_bounds_min, self.y_bounds_max, tolerance)
            if x_bounded or y_bounded:
                self.warnings.add_new('bounds')

        delta_x_inches = x_dest - self.f_curr_x
        delta_y_inches = y_dest - self.f_curr_y

        # Velocity inputs; clarify units.
        vi_inch_per_s = v_i
        vf_inch_per_s = v_f

        # Look at distance to move along 45-degree axes, for native motor steps:
        # Recall that step_scale gives a scaling factor for converting from inches to steps,
        #   *not* native resolution
        # self.step_scale is Either 1016 or 2032, for 8X or 16X microstepping, respectively.

        motor_dist1 = delta_x_inches + delta_y_inches # Inches that belt must turn at Motor 1
        motor_dist2 = delta_x_inches - delta_y_inches # Inches that belt must turn at Motor 2
        motor_steps1 = int(round(self.step_scale * motor_dist1)) # Round to the nearest motor step
        motor_steps2 = int(round(self.step_scale * motor_dist2)) # Round to the nearest motor step

        # Since we are rounding, we need to keep track of the actual distance moved,
        #   not just the _requested_ distance to move.
        motor_dist1_rounded = float(motor_steps1) / (2.0 * self.step_scale)
        motor_dist2_rounded = float(motor_steps2) / (2.0 * self.step_scale)

        # Convert back to find the actual X & Y distances that will be moved:
        delta_x_inches_rounded = (motor_dist1_rounded + motor_dist2_rounded)
        delta_y_inches_rounded = (motor_dist1_rounded - motor_dist2_rounded)

        if abs(motor_steps1) < 1 and abs(motor_steps2) < 1: # If movement is < 1 step, skip it.
            return

        segment_length_inches = plot_utils.distance(delta_x_inches_rounded, delta_y_inches_rounded)

        seg_logger.debug('\ndelta_x_inches Requested: ' + str(delta_x_inches))
        seg_logger.debug('delta_y_inches Requested: ' + str(delta_y_inches))
        seg_logger.debug('motor_steps1: ' + str(motor_steps1))
        seg_logger.debug('motor_steps2: ' + str(motor_steps2))
        seg_logger.debug('\ndelta_x_inches to be moved: ' + str(delta_x_inches_rounded))
        seg_logger.debug('delta_y_inches to be moved: ' + str(delta_y_inches_rounded))
        seg_logger.debug('segment_length_inches: ' + str(segment_length_inches))
        if not self.pen.status.pen_up:
            seg_logger.debug('\nBefore speedlimit check::')
            seg_logger.debug('vi_inch_per_s: {0}'.format(vi_inch_per_s))
            seg_logger.debug('vf_inch_per_s: {0}\n'.format(vf_inch_per_s))
        self.plot_status.stats.add_dist(self.pen.status.pen_up, segment_length_inches)

        # Maximum travel speeds:
        # & acceleration/deceleration rate: (Maximum speed) / (time to reach that speed)
        if self.pen.status.pen_up:
            speed_limit = self.speed_penup
        else:
            speed_limit = self.speed_pendown

        # Acceleration/deceleration rates:
        if self.pen.status.pen_up:
            accel_rate = self.params.accel_rate_pu * self.options.accel / 100.0
        else:
            accel_rate = self.params.accel_rate * self.options.accel / 100.0

        # Maximum acceleration time: Time needed to accelerate from full stop to maximum speed:
        #       v = a * t, so t_max = vMax / a
        # t_max = speed_limit / accel_rate
        # Distance that is required to reach full speed, from zero speed:  x = 1/2 a t^2
        # accel_dist = 0.5 * accel_rate * t_max * t_max

        vi_inch_per_s = min(vi_inch_per_s, speed_limit)
        vf_inch_per_s = min(vf_inch_per_s, speed_limit)

        seg_logger.debug('\nspeed_limit (PlotSegment) ' + str(speed_limit))
        seg_logger.debug('After speedlimit check::')
        seg_logger.debug('vi_inch_per_s: {0}'.format(vi_inch_per_s))
        seg_logger.debug('vf_inch_per_s: {0}\n'.format(vf_inch_per_s))

        # Times to reach maximum speed, from our initial velocity
        # vMax = vi + a*t  =>  t = (vMax - vi)/a
        # vf = vMax - a*t   =>  t = -(vf - vMax)/a = (vMax - vf)/a
        # -- These are _maximum_ values. We often do not have enough time/space to reach full speed.

        t_accel_max = (speed_limit - vi_inch_per_s) / accel_rate
        t_decel_max = (speed_limit - vf_inch_per_s) / accel_rate

        seg_logger.debug('\naccel_rate: {0:.3}'.format(accel_rate))
        seg_logger.debug('speed_limit: {0:.3}'.format(speed_limit))
        seg_logger.debug('vi_inch_per_s: {0}'.format(vi_inch_per_s))
        seg_logger.debug('vf_inch_per_s: {0}'.format(vf_inch_per_s))
        seg_logger.debug('t_accel_max: {0:.3}'.format(t_accel_max))
        seg_logger.debug('t_decel_max: {0:.3}'.format(t_decel_max))

        # Distance that is required to reach full speed, from our start at speed vi_inch_per_s:
        # distance = vi * t + (1/2) a t^2
        accel_dist_max = (vi_inch_per_s * t_accel_max) + (0.5 * accel_rate * t_accel_max * t_accel_max)
        # Use the same model for deceleration distance; modeling it with backwards motion:
        decel_dist_max = (vf_inch_per_s * t_decel_max) + (0.5 * accel_rate * t_decel_max * t_decel_max)

        max_vel_time_estimate = (segment_length_inches / speed_limit)

        seg_logger.debug('accel_dist_max: ' + str(accel_dist_max))
        seg_logger.debug('decel_dist_max: ' + str(decel_dist_max))
        seg_logger.debug('max_vel_time_estimate: ' + str(max_vel_time_estimate))

        # time slices: Slice travel into intervals that are (say) 30 ms long.
        time_slice = self.params.time_slice  # Default slice intervals

        # Declare arrays:
        # These are _normally_ 4-byte integers, but could (theoretically) be 2-byte integers on some systems.
        #   if so, this could cause errors in rare cases (very large/long moves, etc.).
        # Set up an alert system, just in case!

        duration_array = array('I')  # unsigned integer for duration -- up to 65 seconds for a move if only 2 bytes.
        dist_array = array('f')  # float
        dest_array1 = array('i')  # signed integer
        dest_array2 = array('i')  # signed integer

        time_elapsed = 0.0
        position = 0.0
        velocity = vi_inch_per_s

        """
        Next, we wish to estimate total time duration of this segment.
        In doing so, we must consider the possible cases:

        Case 1: 'Trapezoid'
            Segment length is long enough to reach full speed.
            Segment length > accel_dist_max + decel_dist_max
            As a second check, make sure that the segment is long enough that it would take at
            least 4 time slices at maximum velocity.

        Case 2: 'Triangle'
            Segment length is not long enough to reach full speed.
            Accelerate from initial velocity to a local maximum speed,
            then decelerate from that point to the final velocity.

        Case 3: 'Linear velocity ramp'
            For small enough moves -- say less than 10 intervals (typ 500 ms),
            we do not have significant time to ramp the speed up and down.
            Instead, perform only a simple speed ramp between initial and final.

        Case 4: 'Constant velocity'
            Use a single, constant velocity for all pen-down movements.
            Also a fallback position, when moves are too short for linear ramps.

        In each case, we ultimately construct the trajectory in segments at constant velocity.
        In cases 1-3, that set of segments approximates a linear slope in velocity.

        Because we may end up with slight over/undershoot in position along the paths
        with this approach, we perform a final scaling operation (to the correct distance) at the end.
        """


        if not constant_vel_mode or self.pen.status.pen_up:  # Allow accel when pen is up.
            if (segment_length_inches > (accel_dist_max + decel_dist_max + time_slice * speed_limit)
                and max_vel_time_estimate > 4 * time_slice ):
                """
                Case 1: 'Trapezoid'
                """

                seg_logger.debug('Type 1: Trapezoid' + '\n')
                speed_max = speed_limit  # We will reach _full cruising speed_!

                intervals = int(math.floor(t_accel_max / time_slice))  # Number of intervals during acceleration

                # If intervals == 0, then we are already at (or nearly at) full speed.
                if intervals > 0:
                    time_per_interval = t_accel_max / intervals

                    velocity_step_size = (speed_max - vi_inch_per_s) / (intervals + 1.0)
                    # For six time intervals of acceleration, first interval is at velocity (max/7)
                    # 6th (last) time interval is at 6*max/7
                    # after this interval, we are at full speed.

                    for index in range(0, intervals):  # Calculate acceleration phase
                        velocity += velocity_step_size
                        time_elapsed += time_per_interval
                        position += velocity * time_per_interval
                        duration_array.append(int(round(time_elapsed * 1000.0)))
                        dist_array.append(position)  # Estimated distance along direction of travel
                    seg_logger.debug('Accel intervals: ' + str(intervals))

                # Add a center "coasting" speed interval IF there is time for it.
                coasting_distance = segment_length_inches - (accel_dist_max + decel_dist_max)

                if coasting_distance > (time_slice * speed_max):
                    # There is enough time for (at least) one interval at full cruising speed.
                    velocity = speed_max
                    cruising_time = coasting_distance / velocity
                    ct = cruising_time
                    cruise_interval = 20 * time_slice
                    while ct > (cruise_interval):
                        ct -= cruise_interval
                        time_elapsed += cruise_interval
                        duration_array.append(int(round(time_elapsed * 1000.0)))
                        position += velocity * cruise_interval
                        dist_array.append(position)  # Estimated distance along direction of travel

                    time_elapsed += ct
                    duration_array.append(int(round(time_elapsed * 1000.0)))
                    position += velocity * ct
                    dist_array.append(position)  # Estimated distance along direction of travel

                    seg_logger.debug('Coast Distance: ' + str(coasting_distance))
                    seg_logger.debug('Coast velocity: ' + str(velocity))

                intervals = int(math.floor(t_decel_max / time_slice))  # Number of intervals during deceleration

                if intervals > 0:
                    time_per_interval = t_decel_max / intervals
                    velocity_step_size = (speed_max - vf_inch_per_s) / (intervals + 1.0)

                    for index in range(0, intervals):  # Calculate deceleration phase
                        velocity -= velocity_step_size
                        time_elapsed += time_per_interval
                        position += velocity * time_per_interval
                        duration_array.append(int(round(time_elapsed * 1000.0)))
                        dist_array.append(position)  # Estimated distance along direction of travel
                    seg_logger.debug('Decel intervals: ' + str(intervals))

            else:
                """
                Case 2: 'Triangle'

                We will _not_ reach full cruising speed, but let's go as fast as we can!

                We begin with given: initial velocity, final velocity,
                    maximum acceleration rate, distance to travel.

                The optimal solution is to accelerate at the maximum rate, to some maximum velocity
                Vmax, and then to decelerate at same maximum rate, to the final velocity.
                This forms a triangle on the plot of V(t).

                The value of Vmax -- and the time at which we reach it -- may be varied in order to
                accommodate our choice of distance-traveled and velocity requirements.
                (This does assume that the segment requested is self consistent, and planned
                with respect to our acceleration requirements.)

                In a more detail, with short notation Vi = vi_inch_per_s,
                    Vf = vf_inch_per_s, and Amax = accel_rate_local, Dv = (Vf - Vi)

                (i) We accelerate from Vi, at Amax to some maximum velocity Vmax.
                This takes place during an interval of time Ta.

                (ii) We then decelerate from Vmax, to Vf, at the same maximum rate, Amax.
                This takes place during an interval of time Td.

                (iii) The total time elapsed is Ta + Td

                (iv) v = v0 + a * t
                    =>    Vmax = Vi + Amax * Ta
                    and   Vmax = Vf + Amax * Td  (i.e., Vmax - Amax * Td = Vf)

                    Thus Td = Ta - (Vf - Vi) / Amax, or Td = Ta - (Dv / Amax)

                (v) The distance covered during the acceleration interval Ta is given by:
                    Xa = Vi * Ta + (1/2) Amax * Ta^2

                    The distance covered during the deceleration interval Td is given by:
                    Xd = Vf * Td + (1/2) Amax * Td^2

                    Thus, the total distance covered during interval Ta + Td is given by:
                    segment_length_inches = Xa + Xd = Vi * Ta + (1/2) Amax * Ta^2 + Vf * Td + (1/2) Amax * Td^2

                (vi) Now substituting in Td = Ta - (Dv / Amax), we find:
                    Amax * Ta^2 + 2 * Vi * Ta + ( Vi^2 - Vf^2 )/( 2 * Amax ) - segment_length_inches = 0

                    Solving this quadratic equation for Ta, we find:
                    Ta = ( sqrt(2 * Vi^2 + 2 * Vf^2 + 4 * Amax * segment_length_inches) - 2 * Vi ) / ( 2 * Amax )

                    [We pick the positive root in the quadratic formula, since Ta must be positive.]

                (vii) From Ta and part (iv) above, we can find Vmax and Td.
                """

                seg_logger.debug('\nType 2: Triangle')

                if segment_length_inches >= 0.9 * (accel_dist_max + decel_dist_max):
                    accel_rate_local = 0.9 * ((accel_dist_max + decel_dist_max) / segment_length_inches) * accel_rate

                    if accel_dist_max + decel_dist_max == 0:
                        accel_rate_local = accel_rate  # prevent possible divide by zero case, if already at full speed

                    seg_logger.debug('accel_rate_local changed')
                else:
                    accel_rate_local = accel_rate

                if accel_rate_local > 0:  # Edge cases including "already at maximum speed":
                    ta = (math.sqrt(2 * vi_inch_per_s * vi_inch_per_s +
                        2 * vf_inch_per_s * vf_inch_per_s +
                        4 * accel_rate_local * segment_length_inches) -
                        2 * vi_inch_per_s) / (2 * accel_rate_local)
                else:
                    ta = 0

                vmax = vi_inch_per_s + accel_rate_local * ta
                seg_logger.debug('vmax: ' + str(vmax))

                intervals = int(math.floor(ta / time_slice))  # Number of intervals during acceleration

                if intervals == 0:
                    ta = 0

                if accel_rate_local > 0:  # Handle edge cases including when we are already at maximum speed
                    td = ta - (vf_inch_per_s - vi_inch_per_s) / accel_rate_local
                else:
                    td = 0

                d_intervals = int(math.floor(td / time_slice))  # Number of intervals during acceleration

                if intervals + d_intervals > 4:
                    if intervals > 0:
                        seg_logger.debug('Triangle intervals UP: ' + str(intervals))

                        time_per_interval = ta / intervals
                        velocity_step_size = (vmax - vi_inch_per_s) / (intervals + 1.0)
                        # For six time intervals of acceleration, first interval is at velocity (max/7)
                        # 6th (last) time interval is at 6*max/7
                        # after this interval, we are at full speed.

                        for index in range(0, intervals):  # Calculate acceleration phase
                            velocity += velocity_step_size
                            time_elapsed += time_per_interval
                            position += velocity * time_per_interval
                            duration_array.append(int(round(time_elapsed * 1000.0)))
                            dist_array.append(position)  # Estimated distance along direction of travel
                    else:
                        seg_logger.debug('Note: Skipping accel phase in triangle.')

                    if d_intervals > 0:
                        seg_logger.debug('Triangle intervals Down: ' + str(d_intervals))

                        time_per_interval = td / d_intervals
                        velocity_step_size = (vmax - vf_inch_per_s) / (d_intervals + 1.0)
                        # For six time intervals of acceleration, first interval is at velocity (max/7)
                        # 6th (last) time interval is at 6*max/7
                        # after this interval, we are at full speed.

                        for index in range(0, d_intervals):  # Calculate acceleration phase
                            velocity -= velocity_step_size
                            time_elapsed += time_per_interval
                            position += velocity * time_per_interval
                            duration_array.append(int(round(time_elapsed * 1000.0)))
                            dist_array.append(position)  # Estimated distance along direction of travel
                    else:
                        seg_logger.debug('Note: Skipping decel phase in triangle.')
                else:
                    """
                    Case 3: 'Linear or constant velocity changes'

                    Picked for segments that are shorter than 6 time slices.
                    Linear velocity interpolation between two endpoints.

                    Because these are short segments (not enough time for a good "triangle"), we
                    boost the starting speed, by taking its average with vmax for the segment.

                    For very short segments (less than 2 time slices), use a single
                        segment with constant velocity.
                    """

                    seg_logger.debug('Type 3: Linear' + '\n')
                    # xFinal = vi * t  + (1/2) a * t^2, and vFinal = vi + a * t
                    # Combining these (with same t) gives: 2 a x = (vf^2 - vi^2)  => a = (vf^2 - vi^2)/2x
                    # So long as this 'a' is less than accel_rate, we can linearly interpolate in velocity.

                    vi_inch_per_s = (vmax + vi_inch_per_s) / 2
                    velocity = vi_inch_per_s  # Boost initial speed for this segment

                    local_accel = (vf_inch_per_s * vf_inch_per_s - vi_inch_per_s * vi_inch_per_s) /\
                        (2.0 * segment_length_inches)

                    if local_accel > accel_rate:
                        local_accel = accel_rate
                    elif local_accel < -accel_rate:
                        local_accel = -accel_rate
                    if local_accel == 0:
                        # Initial velocity = final velocity -> Skip to constant velocity routine.
                        constant_vel_mode = True
                    else:
                        t_segment = (vf_inch_per_s - vi_inch_per_s) / local_accel

                        intervals = int(math.floor(t_segment / time_slice)) # Number during decel.
                        if intervals > 1:
                            time_per_interval = t_segment / intervals
                            velocity_step_size = (vf_inch_per_s - vi_inch_per_s) / (intervals + 1.0)
                            # For six time intervals of acceleration, first is at velocity (max/7)
                            # 6th (last) time interval is at 6*max/7
                            # after this interval, we are at full speed.

                            for index in range(0, intervals):  # Calculate acceleration phase
                                velocity += velocity_step_size
                                time_elapsed += time_per_interval
                                position += velocity * time_per_interval
                                duration_array.append(int(round(time_elapsed * 1000.0)))
                                dist_array.append(position)  # Distance along direction of travel
                        else: # Short segment; No time for segments at different velocities.
                            vi_inch_per_s = vmax  # These are _slow_ segments;
                            constant_vel_mode = True  #   use fastest possible interpretation.
                            seg_logger.debug('-> [Min-length segment]' + '\n')

        if constant_vel_mode:
            """
            Case 4: 'Constant Velocity mode'
            """

            seg_logger.debug('-> [Constant Velocity Mode Segment]' + '\n')
            # Single segment with constant velocity.

            if self.options.const_speed and not self.pen.status.pen_up:
                velocity = self.speed_pendown  # Constant pen-down speed
            elif vf_inch_per_s > vi_inch_per_s:
                velocity = vf_inch_per_s
            elif vi_inch_per_s > vf_inch_per_s:
                velocity = vi_inch_per_s
            elif vi_inch_per_s > 0:  # Allow case of two are equal, but nonzero
                velocity = vi_inch_per_s
            else:  # Both endpoints are equal to zero.
                velocity = self.speed_pendown / 10
                # TODO: Check this method. May be better to level it out to same value as others.

            seg_logger.debug('velocity: ' + str(velocity))

            time_elapsed = segment_length_inches / velocity
            duration_array.append(int(round(time_elapsed * 1000.0)))
            dist_array.append(segment_length_inches)  # Estimated distance along direction of travel
            position += segment_length_inches

        """
        The time & distance motion arrays for this path segment are now computed.
        Next: We scale to the correct intended travel distance,
        round into integer motor steps and manage the process
        of sending the output commands to the motors.
        """

        seg_logger.debug('position/segment_length_inches: ' + str(position / segment_length_inches))

        for index in range(0, len(dist_array)):
            # Scale our trajectory to the "actual" travel distance that we need:
            fractional_distance = dist_array[index] / position # Position along intended path
            dest_array1.append(int(round(fractional_distance * motor_steps1)))
            dest_array2.append(int(round(fractional_distance * motor_steps2)))
            sum(dest_array1)

        seg_logger.debug('\nSanity check after computing motion:')
        seg_logger.debug('Final motor_steps1: {0:}'.format(dest_array1[-1])) # Last element in list
        seg_logger.debug('Final motor_steps2: {0:}'.format(dest_array2[-1])) # Last element in list

        prev_motor1 = 0
        prev_motor2 = 0
        prev_time = 0

        for index in range(0, len(dest_array1)):
            move_steps1 = dest_array1[index] - prev_motor1
            move_steps2 = dest_array2[index] - prev_motor2
            move_time = duration_array[index] - prev_time
            prev_time = duration_array[index]

            move_time = max(move_time, 1) # don't allow zero-time moves.

            if abs(float(move_steps1) / float(move_time)) < 0.002:
                move_steps1 = 0  # don't allow too-slow movements of this axis
            if abs(float(move_steps2) / float(move_time)) < 0.002:
                move_steps2 = 0  # don't allow too-slow movements of this axis

            # Catch rounding errors that could cause an overspeed event:
            while (abs(float(move_steps1) / float(move_time)) >= self.params.max_step_rate) or\
                (abs(float(move_steps2) / float(move_time)) >= self.params.max_step_rate):
                move_time += 1
                seg_logger.debug('Note: Added delay to avoid overspeed event')

            prev_motor1 += move_steps1
            prev_motor2 += move_steps2

            # If at least one motor step is required for this move, do so:
            if move_steps1 != 0 or move_steps2 != 0:
                motor_dist1_temp = float(move_steps1) / (self.step_scale * 2.0)
                motor_dist2_temp = float(move_steps2) / (self.step_scale * 2.0)

                # X and Y distances moved in this subsegment, inches:
                x_delta = (motor_dist1_temp + motor_dist2_temp)
                y_delta = (motor_dist1_temp - motor_dist2_temp)

                if not self.plot_status.resume.resume_mode and self.plot_status.stopped == 0:

                    f_new_x = self.f_curr_x + x_delta
                    f_new_y = self.f_curr_y + y_delta

                    if self.options.preview:
                        self.plot_status.stats.pt_estimate += move_time
                        if self.options.rendering > 0:  # Generate preview paths
                            if self.vel_data_plot:
                                vel_1 = move_steps1 / float(move_time)
                                vel_2 = move_steps2 / float(move_time)
                                vel_tot = plot_utils.distance(move_steps1,\
                                        move_steps2) / float(move_time)
                                self.update_v_charts(vel_1, vel_2, vel_tot)
                                self.vel_data_time += move_time
                                self.update_v_charts(vel_1, vel_2, vel_tot)
                            if self.rotate_page:
                                if self.params.auto_rotate_ccw: # Rotate counterclockwise 90 degrees
                                    x_new_t = self.svg_width - f_new_y
                                    y_new_t = f_new_x
                                    x_old_t = self.svg_width - self.f_curr_y
                                    y_old_t = self.f_curr_x
                                else:
                                    x_new_t = f_new_y
                                    x_old_t = self.f_curr_y
                                    y_new_t = self.svg_height - f_new_x
                                    y_old_t = self.svg_height - self.f_curr_x
                            else:
                                x_new_t = f_new_x
                                y_new_t = f_new_y
                                x_old_t = self.f_curr_x
                                y_old_t = self.f_curr_y
                            if self.pen.status.pen_up:
                                if self.options.rendering > 1: # Render pen-up movement
                                    if self.pen.status.preview_pen_state != 1:
                                        self.path_data_pu.append("M{0:0.3f} {1:0.3f}".format(
                                            x_old_t, y_old_t))
                                        self.pen.status.preview_pen_state = 1
                                    self.path_data_pu.append(" {0:0.3f} {1:0.3f}".format(
                                        x_new_t, y_new_t))
                            else:
                                if self.options.rendering in [1, 3]: # Render pen-down movement
                                    if self.pen.status.preview_pen_state != 0:
                                        self.path_data_pd.append("M{0:0.3f} {1:0.3f}".format(
                                            x_old_t, y_old_t))
                                        self.pen.status.preview_pen_state = 0
                                    self.path_data_pd.append(" {0:0.3f} {1:0.3f}".format(
                                        x_new_t, y_new_t))
                    else:
                        ebb_motion.doXYMove(self.plot_status.port, move_steps2, move_steps1,\
                            move_time, False)
                        self.plot_status.progress.update(self.node_count)
                        if move_time > 50: # Sleep before issuing next command
                            if self.options.mode != "manual":
                                time.sleep(float(move_time - 30) / 1000.0)
                    seg_logger.debug(f'XY move: ({move_steps1}, {move_steps2}), in {move_time} ms')
                    seg_logger.debug(f'fNew(X,Y): ({f_new_x:.2}, {f_new_y:.2})')
                    if (move_steps1 / move_time) >= self.params.max_step_rate:
                        seg_logger.debug('Motor 1 overspeed error.')
                    if (move_steps2 / move_time) >= self.params.max_step_rate:
                        seg_logger.debug('Motor 2 overspeed error.')

                    self.f_curr_x = f_new_x  # Update current position
                    self.f_curr_y = f_new_y

                    self.svg_last_known_pos_x = self.f_curr_x - self.pt_first[0]
                    self.svg_last_known_pos_y = self.f_curr_y - self.pt_first[1]

    def pause_res_check(self):
        """ Manage Pause & Resume functionality """
        # First check to see if the pause button has been pressed. Increment the node counter.
        # Also, resume drawing if we _were_ in resume mode and need to resume at this node.

        if self.plot_status.stopped > 0:
            return  # Plot is already stopped. No need to proceed.

        if self.options.preview:
            str_button = 0
        else: # Query button press
            str_button = ebb_motion.QueryPRGButton(self.plot_status.port, False)

        # To test corner cases of pause and resume cycles, one may manually force a pause:
        # if (self.options.mode == "plot") and (self.node_count == 24):
        #     self.plot_status.stopped = -1 # Flag to request programmatic pause

        if self.receive_pause_request(): # Keyboard interrupt detected!
            self.plot_status.stopped = -103 # Code 104: "Keyboard interrupt"
            if self.plot_status.delay_between_copies: # However... it could have been...
                self.plot_status.stopped = -2 # Paused between copies (OK).

        if self.plot_status.stopped == -1:
            self.user_message_fun('Plot paused programmatically.\n')
        if self.plot_status.stopped == -103:
            self.user_message_fun('\nPlot paused by keyboard interrupt.\n')

        pause_button_pressed = 0
        if self.plot_status.stopped == 0 and self.plot_status.port is not None:
            try:
                pause_button_pressed = int(str_button[0])
            except (IndexError, ValueError):
                self.user_message_fun(\
                    f'\nError: USB connection to AxiDraw lost. [Node {self.node_count}]\n')
                self.connected = False # Python interactive API variable
                self.plot_status.stopped = -104 # Code 104: "Lost connectivity"

        if pause_button_pressed == 1:
            if self.plot_status.delay_between_copies:
                self.plot_status.stopped = -2 # Paused between copies.
            elif self.options.mode == "interactive":
                logger.warning('Plot halted by button press during interactive session.')
                logger.warning('Manually home this AxiDraw before plotting next item.\n')
                self.plot_status.stopped = -102 # Code 102: "Paused by button press"
            else:
                self.user_message_fun('Plot paused by button press.\n')
                self.plot_status.stopped = -102 # Code 102: "Paused by button press"

        if self.plot_status.stopped == -2:
            self.user_message_fun('Plot sequence ended between copies.\n')

        if self.plot_status.stopped:
            logger.debug('\n (Paused after node number : ' + str(self.node_count) + ')')
            if self.options.mode == "res_plot": #Check for a special case:
                if self.node_count < self.node_target: # If Paused again before resuming,
                    self.node_count = self.node_target # Skip to end of resume mode.

        if self.plot_status.stopped < 0: # Stop plot
            self.svg_node_count = self.node_count
            self.svg_paused_x = self.f_curr_x - self.pt_first[0]
            self.svg_paused_y = self.f_curr_y - self.pt_first[1]
            v_time = self.pen.pen_raise(self.options, self.params, self.plot_status)
            self.v_chart_rest(v_time)
            if not self.plot_status.delay_between_copies and \
                not self.Secondary and self.options.mode != "interactive":
                # Only print if we're not in the delay between copies, nor a "second" unit.
                if self.plot_status.stopped != -104: # Do not display after loss of USB.
                    self.user_message_fun('Use the resume feature to continue.\n')
            self.plot_status.stopped = - self.plot_status.stopped
            return  # Note: This segment is not plotted.

        self.node_count += 1  # This whole segment move counts as ONE pause/resume node in our plot

        if self.plot_status.progress.enable and self.options.preview:
            if self.node_count > self.plot_status.progress.total:
                self.plot_status.progress.total = self.node_count

        if self.plot_status.resume.resume_mode:
            if self.node_count >= self.node_target:
                self.plot_status.resume.resume_mode = False

                logger.debug('\nRESUMING PLOT at node : ' + str(self.node_count))
                logger.debug('\nself.virtual_pen_up : ' + str(self.pen.status.virtual_pen_up))
                logger.debug('\nself.pen.status.pen_up : ' + str(self.pen.status.pen_up))

                # Set pen heights, in case it has been changed by layers in resume mode:
                self.pen.servo_setup(self.options, self.params, self.plot_status)
                if not self.pen.status.virtual_pen_up:  # Switch from virtual to real pen
                    v_time = self.pen.pen_lower(self.options, self.params, self.plot_status)
                    self.v_chart_rest(v_time)

    def serial_connect(self):
        """ Connect to AxiDraw over USB """
        if serial_utils.connect(self.options, self.plot_status, self.user_message_fun, logger):
            self.connected = True  # Variable available in the Python interactive API.
        else:
            self.plot_status.stopped = 101 # Will become exit code 101; failed to connect

    def enable_motors(self):
        """
        Enable motors, set native motor resolution, and set speed scales.
        The "pen down" speed scale is adjusted by reducing speed when using 8X microstepping or
        disabling aceleration. These factors prevent unexpected dramatic changes in speed when
        turning those two options on and off.
        """
        if self.use_layer_speed:
            local_speed_pendown = self.layer_speed_pendown
        else:
            local_speed_pendown = self.options.speed_pendown

        if self.options.resolution == 1:  # High-resolution ("Super") mode
            if not self.options.preview:
                res_1, res_2 = ebb_motion.query_enable_motors(self.plot_status.port, False)
                if not (res_1 == 1 and res_2 == 1): # Do not re-enable if already enabled
                    ebb_motion.sendEnableMotors(self.plot_status.port, 1)  # 16X microstepping
            self.step_scale = 2.0 * self.params.native_res_factor
            self.speed_pendown = local_speed_pendown * self.params.speed_lim_xy_hr / 110.0
            self.speed_penup = self.options.speed_penup * self.params.speed_lim_xy_hr / 110.0
            if self.options.const_speed:
                self.speed_pendown = self.speed_pendown * self.params.const_speed_factor_hr
        else:  # i.e., self.options.resolution == 2; Low-resolution ("Normal") mode
            if not self.options.preview:
                res_1, res_2 = ebb_motion.query_enable_motors(self.plot_status.port, False)
                if not (res_1 == 2 and res_2 == 2): # Do not re-enable if already enabled
                    ebb_motion.sendEnableMotors(self.plot_status.port, 2)  # 8X microstepping
            self.step_scale = self.params.native_res_factor
            # Low-res mode: Allow faster pen-up moves. Keep maximum pen-down speed the same.
            self.speed_penup = self.options.speed_penup * self.params.speed_lim_xy_lr / 110.0
            self.speed_pendown = local_speed_pendown * self.params.speed_lim_xy_lr / 110.0
            if self.options.const_speed:
                self.speed_pendown = self.speed_pendown * self.params.const_speed_factor_lr
        if self.params.use_b3_out:
            ebb_motion.PBOutConfig(self.plot_status.port, 3, 0, False) # I/O Pin B3 -> output, low

    def query_ebb_voltage(self):
        """ Check that power supply is detected. """
        serial_utils.query_voltage(self.options, self.params, self.plot_status, self.warnings)

    def get_doc_props(self):
        """
        Get the document's height and width attributes from the <svg> tag. Use a default value in
        case the property is not present or is expressed in units of percentages.
        """

        self.svg_height = plot_utils.getLengthInches(self, 'height')
        self.svg_width = plot_utils.getLengthInches(self, 'width')

        width_string = self.svg.get('width')
        if width_string:
            _value, units = plot_utils.parseLengthWithUnits(width_string)
            self.doc_units = units
        if self.svg_height is None or self.svg_width is None:
            return False
        if self.options.no_rotate: # Override regular auto_rotate option
            self.options.auto_rotate = False
        if self.options.auto_rotate and (self.svg_height > self.svg_width):
            self.rotate_page = True
        return True

    def get_output(self):
        """Return serialized copy of svg document output"""
        result = etree.tostring(self.document)
        return result.decode("utf-8")

    def disconnect(self):
        '''End serial session; disconnect from AxiDraw '''
        if self.plot_status.port:
            ebb_serial.closePort(self.plot_status.port)
        self.plot_status.port = None
        self.connected = False  # Python interactive API variable

class SecondaryLoggingHandler(logging.Handler):
    '''To be used for logging to AxiDraw.text_out and AxiDraw.error_out.'''
    def __init__(self, axidraw, log_name, level = logging.NOTSET):
        super(SecondaryLoggingHandler, self).__init__(level=level)

        log = getattr(axidraw, log_name) if hasattr(axidraw, log_name) else ""
        setattr(axidraw, log_name, log)

        self.axidraw = axidraw
        self.log_name = log_name

        self.setFormatter(logging.Formatter()) # pass message through unchanged

    def emit(self, record):
        assert(hasattr(self.axidraw, self.log_name))
        new_log = getattr(self.axidraw, self.log_name) + "\n" + self.format(record)
        setattr(self.axidraw, self.log_name, new_log)

class SecondaryErrorHandler(SecondaryLoggingHandler):
    '''Handle logging for "secondary" machines, plotting alongside primary.'''
    def __init__(self, axidraw):
        super(SecondaryErrorHandler, self).__init__(axidraw, 'error_out', logging.ERROR)

class SecondaryNonErrorHandler(SecondaryLoggingHandler):
    class ExceptErrorsFilter(logging.Filter):
        def filter(self, record):
            return record.levelno < logging.ERROR

    def __init__(self, axidraw):
        super(SecondaryNonErrorHandler, self).__init__(axidraw, 'text_out')
        self.addFilter(self.ExceptErrorsFilter())

if __name__ == '__main__':
    logging.basicConfig()
    e = AxiDraw()
    exit_status.run(e.affect)
