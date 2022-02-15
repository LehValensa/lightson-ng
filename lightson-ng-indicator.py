#!/usr/bin/env python3

"""
  Status indicator GUI for lightson-ng.
  URL: https://github.com/LehValensa/lightson-ng

  Copyright (c) 2022 grytsenko.alexander at gmail com
  This script is licensed under GNU GPL version 2.0 or above

  Based on:
  launcher_indicator.py by @author: HUC Stéphane, @email: <devs@stephane-huc.net>, @url: http://stephane-huc.net
"""


import sys
import time
import subprocess
import signal
from threading import Thread
# import traceback
from queue import Queue, Empty

import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gio, GLib, Gdk, Pango

gi.require_version('AppIndicator3', '0.1')
from gi.repository import AppIndicator3


# To suppress warning: DeprecationWarning: AppIndicator3.Indicator.set_icon is deprecated
# FIXME: I have no idea yet how to update the icon in else way, without using deprecated function
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning, message='AppIndicator3.Indicator.set_icon is deprecated')

# Definition of dbus service is here.
# Take the necessary only.

statModule = __import__("lightson-ng-stat")
# Create shortcuts
log = statModule.log
# OPTIMIZE: log_error is extended in LightsonIndicator. Create class for logging.
log_error_stat = statModule.log_error
parse_command_line = statModule.parse_command_line
IF_NAME = statModule.IF_NAME
SRV_NAME = statModule.SRV_NAME
OBJ_NAME = statModule.OBJ_NAME
StatObject = statModule.StatObject
SYSTEMD_LIGHTSON_SERVICE = statModule.SYSTEMD_LIGHTSON_SERVICE
SERVICE_OPERATION_TIMEOUT = statModule.SERVICE_OPERATION_TIMEOUT

"""
# Unwrap the dbus.Dictionary array.
def unwrap_dbus(val):
    if isinstance(val, dbus.ByteArray):
        return "".join([str(x) for x in val])
    if isinstance(val, (dbus.Array, list, tuple)):
        return [unwrap_dbus(x) for x in val]
    if isinstance(val, (dbus.Dictionary, dict)):
        return dict([(unwrap_dbus(x), unwrap_dbus(y)) for x, y in val.items()])
    if isinstance(val, (dbus.Signature, dbus.String)):
        return str(val)
    if isinstance(val, dbus.Boolean):
        return bool(val)
    if isinstance(val, (dbus.Int16, dbus.UInt16, dbus.Int32, dbus.UInt32, dbus.Int64, dbus.UInt64)):
        return int(val)
    if isinstance(val, dbus.Byte):
        return bytes([int(val)])
    return val
"""


class LightsonIndicator:
    """
    Lightson-ng-indicator main application object: indicates the status of lightson-ng backend.
    Creates GUI as Gnome's application indicator, adds the menu, defines actions on keys pressed,
    connects to dbus as client, listens to dbus signals and executes corresponding actions upon receive:
        - changes visual status of indicator, including label text and icon
        - shows notification window with details of status
        - shows misc statistics from lightson-ng.
        - controls start/stop of lightson-ng service
    """
    lightson_bus = None
    lightson_proxy = None
    lightson_iface = None
    notification_bus = None
    notification_proxy = None
    systemd_bus = None
    systemd_proxy = None
    app_id = None
    app_indicator = None
    about_dialog = None
    stats_dialog = None
    stats_all = None
    dbus_error = False
    log_win = None
    current_icon = 'dialog-information'
    update_blink = None

    def __init__(self):

        # Create menu and launch indicator in the panel.
        self.setup_gui()

        # Connect to freedesktop notification bus
        self.init_notification()

        # Connect to lightson-ng DBUS
        try:
            self.dbus_reconnect_client()
        except (ValueError, Exception):
            self.log_error("backend is not running. Please start lighton-ng service.")

        # Initial refresh to get a current status of lightson.
        # Also, show the notification window.
        try:
            # The initial stats refresh
            self.iteration_finished_action()
        except (ValueError, Exception):
            self.log_error("can not execute iteration_finished_action.")

    def call_dbus_method(self, method_name, is_ping=False):
        """
        Synchronously call the method from dbus service.
        Some details about service calling dbus methods:
        with Gio it is Ok to call the method directly, like this:
               self.lightson_proxy.GetDisableReasons()
        But it is safer to use call_sync(), I believe.
        Such call returns an array like this: response['0']['disableReason_sleep']
        Another example, with passing parameters:
                        proxy.call_sync(“Configure”,
                                        GLib.Variant("(u)", (int(sys.argv[1]),)),
                                        Gio.DBusCallFlags.NONE,
                                        -1,
                                        None)
        :param is_ping: check if connection is alive
        :param method_name: the name of dbus method.
        :return: the data returned by dbus method
        """

        # Try to reconnect to dbus, hoping that previous problem with dbus has gone.
        if self.dbus_error and not is_ping:
            self.dbus_reconnect_client()

        try:
            return_data = self.lightson_proxy.call_sync(method_name,
                                                        None,
                                                        Gio.DBusCallFlags.NONE,
                                                        -1,
                                                        None)
        except (ValueError, Exception):
            self.set_icon("dialog-error")
            raise

        return return_data

    def connect_to_proxy_object(self, bus_type):
        """
        Connect to the DBUS service via proxy, check if service is alive
        :param bus_type: type of DBUS - Gio.BusType.SYSTEM or Gio.BusType.SESSION.
        :return:
        """
        try:
            # Connect to dbus
            self.lightson_bus = Gio.bus_get_sync(bus_type)
        except (ValueError, Exception):
            raise

        try:
            # Connect to Lightson-ng interface
            # Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES
            self.lightson_proxy = Gio.DBusProxy.new_sync(self.lightson_bus, Gio.DBusProxyFlags.NONE, None,
                                                         SRV_NAME, OBJ_NAME, IF_NAME, None)
        except (ValueError, Exception):
            raise

        try:
            # Ping the object by calling its method.
            reply = self.call_dbus_method("PingStats", is_ping=True)[0]
            if reply == "Hello":
                log("Ping OK")

        except (ValueError, Exception):
            raise

    def dbus_reconnect_client(self):
        """
        Connect/reconnect to lightson-ng dbus service
        :return:
        """

        # Search the service in system bus first, then in session bus.
        log("Connecting to dbus service")

        try:
            self.connect_to_proxy_object(Gio.BusType.SYSTEM)
        except (ValueError, Exception):
            try:
                self.connect_to_proxy_object(Gio.BusType.SESSION)
            except (ValueError, Exception):
                log("can not find " + SRV_NAME + " service in DBUS.")
                self.dbus_error = True
                raise

        log("Connecting to dbus signals")
        try:
            # Listen to signals from proxy.
            # noinspection PyUnresolvedReferences
            self.lightson_proxy.connect("g-signal", self.on_signal_receive)
        except (ValueError, Exception):
            log("can not connect to signals from lightson")
            self.dbus_error = True
            raise

        self.dbus_error = False

    def init_notification(self):
        """
        Connect to Desktop Notification interface
        :return:
        """
        try:
            self.notification_bus = Gio.bus_get_sync(Gio.BusType.SESSION)
            self.notification_proxy = Gio.DBusProxy.new_sync(self.notification_bus,
                                                             Gio.DBusProxyFlags.NONE,
                                                             None,
                                                             "org.freedesktop.Notifications",
                                                             "/org/freedesktop/Notifications",
                                                             "org.freedesktop.Notifications",
                                                             None)
        except (ValueError, Exception):
            self.log_error("can not connect to Notifications.")
            raise

    def setup_gui(self):
        """
        Create application, add the menu, setup initial icon
        :return:
        """

        self.app_id = 'lightson-ng-indicator'
        # noinspection PyArgumentList
        self.app_indicator = AppIndicator3.Indicator.new(
            id=self.app_id, icon_name=self.current_icon,
            category=AppIndicator3.IndicatorCategory.APPLICATION_STATUS)
        self.app_indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

        # Set the location of lightson's icons, set the initial icon
        # self.app_indicator.set_icon_theme_path(os.path.dirname(os.path.realpath(__file__)) + "/icons")
        log("icon_theme_path: " + str(self.app_indicator.get_icon_theme_path()))

        menu = Gtk.Menu()
        # menu item 1
        item_force_check = Gtk.MenuItem(label='Force check')
        item_force_check.connect('activate', self.on_check)
        menu.append(item_force_check)

        item_show_stats = Gtk.MenuItem(label='Show stats')
        item_show_stats.connect('activate', self.on_show_stats)
        menu.append(item_show_stats)

        item_show_logs = Gtk.MenuItem(label='Show logs')
        item_show_logs.connect('activate', self.on_show_logs)
        menu.append(item_show_logs)

        menu_sep = Gtk.SeparatorMenuItem()
        # noinspection PyTypeChecker
        menu.append(menu_sep)

        item_start_service = Gtk.MenuItem(label='Start service')
        item_start_service.connect('activate', self.on_start_service)
        menu.append(item_start_service)

        item_stop_service = Gtk.MenuItem(label='Stop service')
        item_stop_service.connect('activate', self.on_stop_service)
        menu.append(item_stop_service)

        menu_sep = Gtk.SeparatorMenuItem()
        # noinspection PyTypeChecker
        menu.append(menu_sep)

        item_about = Gtk.MenuItem(label='About')
        item_about.connect('activate', self.on_about)
        menu.append(item_about)

        item_quit = Gtk.MenuItem(label='Quit')
        item_quit.connect('activate', self.on_quit)
        menu.append(item_quit)

        menu.show_all()
        self.app_indicator.set_menu(menu)
        self.app_indicator.set_title("lightson-ng-indicator")
        # Run check on mouse middle-button click
        self.app_indicator.set_secondary_activate_target(item_force_check)

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def on_about(self, source):
        """
        Display about window
        :param source:
        :return:
        """
        log("Showing about window")
        try:
            self.about_dialog.present()
        except AttributeError:
            self.about_dialog = Gtk.AboutDialog()

            self.about_dialog.set_logo_icon_name('io.github.ImEditor')
            self.about_dialog.set_program_name('lightson-ng-indicator')
            self.about_dialog.set_version('0.0.1')
            self.about_dialog.set_website('https://lightson-ng.github.io')
            self.about_dialog.set_authors(['Alexander Grytsenko'])
            gtk_version = '{}.{}.{}'.format(Gtk.get_major_version(),
                                            Gtk.get_minor_version(), Gtk.get_micro_version())
            comment = '{}\n\n'.format("GUI for lightson-ng service - idle/sleep mode prevention")
            comment += 'Gtk: {}'.format(gtk_version)
            self.about_dialog.set_comments(comment)
            text = "Distributed under the GNU GPL(v3) license.\n"
            text += 'https://github.com/ligtson-ng/blob/master/LICENSE\n'
            self.about_dialog.set_license(text)
            self.about_dialog.run()
            self.about_dialog.destroy()
            del self.about_dialog

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def on_quit(self, source):
        """
        Quit program
        :param source:
        :return:
        """
        Gtk.main_quit()

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def on_check(self, source):
        """
        Ask the lightson-ng to perform checks
        :param source:
        :return:
        """
        log("Sending the signal to perform checks.")

        try:
            # Ask lightson-ng to perform new checks.
            self.call_dbus_method("ForceNewIteration")
        except (ValueError, Exception):
            self.log_error("can not force new iteration", )
            raise

        self.update_blink = Thread(target=self.blinking_icon)
        self.update_blink.start()

    # noinspection PyArgumentList
    def blinking_icon(self):
        """
        Blink the icon to inform the user that something is going on:
        display a different icons within a short delay. Blinking stops after 3 sec.
        Blinking icons are not saved permanently, so they do not interfere
        with permanent status icons.
        The current permanent icon is restored after blinking is finished.
        :return:
        """

        for i in range(3):
            GLib.idle_add(
                self.set_icon,
                "emblem-synchronizing",
                True,
                priority=GLib.PRIORITY_DEFAULT
            )
            time.sleep(.5)
            GLib.idle_add(
                self.set_icon,
                self.current_icon,
                True,
                priority=GLib.PRIORITY_DEFAULT
            )
            time.sleep(.5)

        # Restore the current icon
        GLib.idle_add(
            self.set_icon,
            self.current_icon,
            False,
            priority=GLib.PRIORITY_DEFAULT
        )

    # noinspection PyUnusedLocal
    def on_show_stats(self, source):
        """
        Display the window with lightson-ng statistics
        :param source:
        :return:
        """
        log("Showing statistics window")

        try:
            # refresh statistics
            self.stats_all = self.call_dbus_method("GetStats")[0]
        except (ValueError, Exception):
            self.log_error("can not get statistics")
            raise

        # log(self.stats_all)
        win = LightsonStatisticsWindow(self.stats_all)

    # noinspection PyUnresolvedReferences
    def systemd_operation(self, action):
        """
        Start/stop lightson-ng service
        :param action:
        :return:
        """
        result = None
        mode = "fail"

        try:
            self.systemd_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
            self.systemd_proxy = Gio.DBusProxy.new_sync(self.systemd_bus,
                                                        Gio.DBusProxyFlags.NONE,
                                                        None,
                                                        "org.freedesktop.systemd1",
                                                        "/org/freedesktop/systemd1",
                                                        "org.freedesktop.systemd1.Manager",
                                                        None)

            if action == "start":
                result = self.systemd_proxy.StartUnit("(ss)", SYSTEMD_LIGHTSON_SERVICE, mode)
            elif action == "stop":
                result = self.systemd_proxy.StopUnit("(ss)", SYSTEMD_LIGHTSON_SERVICE, mode)
            elif action == "restart":
                result = self.systemd_proxy.RestartUnit("(ss)", SYSTEMD_LIGHTSON_SERVICE, mode)
        except (ValueError, Exception):
            log(f"can not execute systemd command {action} for {SYSTEMD_LIGHTSON_SERVICE} service ")
            raise

        log("systemd result: " + str(result))

    # noinspection PyUnusedLocal
    def on_start_service(self, source):
        """
        Start the lightson-ng service manually.
        Note: normally it should be started by systemd at boot.
        :return:
        """
        self.systemd_operation("start")

        timeout = SERVICE_OPERATION_TIMEOUT  # [seconds]
        timeout_start = time.time()
        timeout_stop = timeout_start + timeout

        while time.time() < timeout_stop:
            log("waiting for service start... until timeout: "
                + str(int(timeout_stop - time.time())) + " sec.")
            try:
                self.dbus_reconnect_client()
            except (ValueError, Exception):
                log("no luck...")
                pass
            if not self.dbus_error:

                # Refresh the statistics on service startup
                # OPTIMIZE: do we need this extra check? Isn't lightson-ng provides stats right after start?
                self.on_check("ServiceStartup")
                return

            time.sleep(1)

        log("timeout reached")

    # noinspection PyUnusedLocal
    def on_stop_service(self, source):
        """
        Stop lightson-ng service.
        :return:
        """
        self.systemd_operation("stop")

        timeout = SERVICE_OPERATION_TIMEOUT  # [seconds]
        timeout_start = time.time()
        timeout_stop = timeout_start + timeout

        while time.time() < timeout_stop:
            log("waiting for service shutdown... until timeout: "
                + str(int(timeout_stop - time.time())) + " sec.")
            try:
                self.dbus_reconnect_client()
            except (ValueError, Exception):
                log("service stopped")
                return

            time.sleep(1)

        log("timeout reached")

    # noinspection PyUnusedLocal
    def on_show_logs(self, source):
        """
        Display last logs of lightson-ng process itself and its dbus service in the new window.
        :return:
        """
        self.log_win = LightsonLogsWindow()

    # noinspection PyUnusedLocal
    def on_signal_receive(self, proxy, sender, signal_name, args):
        """
        Parse all signals received from lightson-ng service
        :param proxy:
        :param sender:
        :param signal_name:
        :param args:
        :return:
        """
        log(f"Received signal: {signal_name}")

        if signal_name == "IterationFinishedSignal":
            self.iteration_finished_action()

    def iteration_finished_action(self):
        """
        Lightson's checks are finished. Display the result by updating status icon and label.

        Display "X" in the label if reason is disabled.
        First character corresponds to the idle reason, second - to the sleep reason.
        Update the icon:
            - set "non-starred" if no disable reasons found
            - set "semi-starred" if disable reason is found for one state, but not found for another
            - set "starred" if disable reasons were found for all states.
            - set "dialog-warning" if a reason was not parsed correctly.
            Note: the icon can be also set in call_dbus_method(): when connection fails,
                then "dialog-error" icon is set.

        Send the desktop notification (for verbose mode only) with details of status.
        :return:
        """
        log("Executing iteration_finished_action()")

        self.stats_all = self.call_dbus_method("GetStats")[0]

        if len(self.stats_all['disableReason_idle']) > 0:
            label_text = "X"
        else:
            label_text = "-"

        if len(self.stats_all['disableReason_sleep']) > 0:
            label_text = label_text + "X"
        else:
            label_text = label_text + "-"

        if label_text == "--":
            self.set_icon("non-starred")
        elif label_text == "XX":
            self.set_icon("starred")
        elif "-" in label_text:
            self.set_icon("semi-starred")
        else:
            log("current label:" + label_text)
            self.set_icon("dialog-warning")

        if int(self.stats_all["runtimeErrors"]) > 0:
            label_text = "ERR"

        self.app_indicator.set_label(label_text, self.app_id)
        log("label_text: " + label_text)

        self.send_notification("Checks status: [" + label_text + "] Disable reasons: ",
                               "For idle mode: [" + self.stats_all['disableReason_idle'] + "]" + "\n" +
                               "For sleep mode: [" + self.stats_all['disableReason_sleep'] + "]"
                               )

    def send_notification(self, header, text):
        """
        Display notification window on desktop. "org.freedesktop.Notifications" service is used.
        Note: the first parameter to Notify() is method signature that should strictly
        correspond to declaration of this method on the service side.
        Example for one simple string: "(s)". Not clear why parenthesis are needed, but it is a must.
        Note: variant values should be wrapped using Glib.Variant()
        :param header: text for window header
        :param text: notification text
        :return:
        """

        if cmdline.verbose:
            # noinspection PyUnresolvedReferences
            self.notification_proxy.Notify("(susssasa{sv}i)",
                                           "lightson-ng-indicator",
                                           12345,
                                           "",
                                           header,
                                           text,
                                           [],
                                           {"urgency": GLib.Variant("i", 1)},
                                           3000)

    def set_icon(self, icon_name, is_temporary=False):
        """
        Update the icon of indicator with a new one.

        The following gnome's icons are used to display current lighton-ng status:
        "non-starred"       - none of "sleep" or "idle" mode is disabled.
        "semi-starred"      - either "sleep" or "idle" mode is disabled.
        "starred"           - both "sleep" and "idle" modes are disabled.
        "dialog-error"      - when a critical error encountered
        "dialog-warning"    - when lightson-ng is running on SESSION bus, not SYSTEM bus. Unused.

        Note: this complication (over simple set_icon() call) is written for future "stability" of the code,
                since at present time Gnome does not provide clarity on how to update the icon:
                the old method is deprecated, no new method proposed, except using Notifications,
                which is not quite applicable to lightson's logic
        :param icon_name: the name of icon (filename without extension)
        :param is_temporary: True - do not save this icon as permanent. Used by blinking_icon()
        :return:

        Hint: use gtk3-icon-browser from gtk-3-examples package to locate an icon.
        Application status is supported by AppIndicator3, but lightson needs more statuses.
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ATTENTION)
        self.ind.set_icon(os.path.join(_curr_dir, 'pics', 'tools.png'))
        self.ind.set_attention_icon (os.path.join(_curr_dir, 'pics', 'tools-active.png'))
        """

        # Try the standard gnome's path, if not found - use icons from predefined directory
        # (uncomment set_icon_theme_path() in setup_gui()).
        if self.app_indicator.get_icon_theme_path() is None:
            self.app_indicator.set_icon(icon_name)
        else:
            self.app_indicator.set_icon_full(str(self.app_indicator.get_icon_theme_path()) + "/" + icon_name + ".png",
                                             icon_name)
        if not is_temporary:
            self.current_icon = icon_name

    def log_error(self, message):
        """
        Log the error to:
            - desktop notification. Log contains the message itself and a short exception info.
            - the screen (if --verbose option used). Log contains the message itself and the full exception stack
            - to syslog. (if --verbose option used). Log contains the message itself only
        :param message:
        :return:
        """
        log_error_stat(message)
        self.send_notification("ERROR: lightson-ng-indicator", message + "     " + str(sys.exc_info()[1]))


# OPTIMIZE: it would be nice to see tabs instead of buttons
# noinspection PyArgumentList
class LightsonStatisticsWindow(Gtk.Window):
    """
    The window containing statistics collected by lightson-ng.
    A several tabs are selectable:
    Default view - valuable data
    All - all stats collected
    disableReason - all disable reasons
    checkPerformed - all checks performed
    """

    def __init__(self, stats_all):
        """
        Display a window with statistics
        :param stats_all: dictionary with stats collected.
        """
        super().__init__(title="lightson-ng statistics: default view")

        # Setting up statistics window
        self.set_title("lightson-ng statistics: default view")
        self.set_default_size(600, 700)
        self.set_border_width(10)
        self.connect("key-press-event", self.on_key_press_event)

        # Setting up the self.grid in which the elements are to be positioned
        self.grid = Gtk.Grid()
        self.grid.set_column_homogeneous(True)
        self.grid.set_row_homogeneous(True)
        self.add(self.grid)

        # Creating the ListStore model
        self.list_store = Gtk.ListStore(str, str)

        # Put all stats into model
        for Key, Value in sorted(stats_all.items()):
            self.list_store.append([Key, Value])

        # Creating the filter, feeding it with the list_store model
        self.current_filter_key = "Default view"
        self.key_filter = self.list_store.filter_new()
        # Setting the filter function
        self.key_filter.set_visible_func(self.stats_dialog_filter)

        # Creating the treeview, making it use the filter as a model, and adding the columns
        self.treeview = Gtk.TreeView(model=self.key_filter)

        # Create two columns with one shared renderer and a shared set_cell_data_func().
        renderer_text = Gtk.CellRendererText()
        column_text1 = Gtk.TreeViewColumn("Key", renderer_text, text=0)
        column_text2 = Gtk.TreeViewColumn("Value", renderer_text, text=1)
        column_text1.set_cell_data_func(renderer_text, self.reason_highlight)
        column_text2.set_cell_data_func(renderer_text, self.reason_highlight)
        self.treeview.append_column(column_text1)
        self.treeview.append_column(column_text2)

        # creating buttons to filter by programming language, and setting up their events
        self.buttons = list()
        for stats_key in ["Default view", "disableReason", "checkPerformed", "All stats"]:
            button = Gtk.Button(label=stats_key)
            self.buttons.append(button)
            button.connect("clicked", self.on_stats_selection_button_clicked)

        # setting up the layout, putting the treeview in a scroll-window, and the buttons in a row
        self.scrollable_tree_list = Gtk.ScrolledWindow()
        self.scrollable_tree_list.set_vexpand(True)
        self.grid.attach(self.scrollable_tree_list, 0, 0, 8, 10)

        # One more button to close the window.
        # It appears apart from other buttons.
        button = Gtk.Button(label="Close")
        self.grid.attach_next_to(
            button, self.scrollable_tree_list, Gtk.PositionType.RIGHT, 1, 1
        )
        button.connect("clicked", self.destroy_stats_dialog)

        # Add the first filter button to the grid.
        self.grid.attach_next_to(
            self.buttons[0], self.scrollable_tree_list, Gtk.PositionType.BOTTOM, 1, 1
        )

        # Add the rest filter buttons to the grid.
        for i, button in enumerate(self.buttons[1:]):
            self.grid.attach_next_to(
                button, self.buttons[i], Gtk.PositionType.RIGHT, 1, 1
            )
        self.scrollable_tree_list.add(self.treeview)

        self.show_all()
        self.connect("destroy", self.destroy_stats_dialog)

    # noinspection PyMethodMayBeStatic, PyUnusedLocal
    def reason_highlight(self, column, renderer, model, iter_index, extra_param):
        """Mark the main nonempty disable reasons bold"""

        stats_key = model[iter_index][0]
        stats_value = model[iter_index][1]

        if (stats_key == "disableReason_idle" or stats_key == "disableReason_sleep") \
                and len(stats_value) > 0:

            # Switch from plain text to markup which has extended capabilities
            renderer.set_property("text", None)

            # Check which column called this renderer and set the emphasized value for corresponding column
            if column.get_property("title") == "Key":
                renderer.set_property("markup", "<b>" + stats_key + "</b>")
            else:
                renderer.set_property("markup", "<b>" + stats_value + "</b>")

    # noinspection PyUnusedLocal
    def destroy_stats_dialog(self, widget):
        """Close statistics window"""
        self.destroy()

    # noinspection PyUnusedLocal
    def stats_dialog_filter(self, model, iter_index, data):
        """Tests if the stats key in the row is the one in the filter"""

        stats_key = model[iter_index][0]
        stats_value = model[iter_index][1]

        if (
                self.current_filter_key is None
                or self.current_filter_key == "None"
        ):
            return False

        # No filter at all - show everything.
        elif self.current_filter_key == "All stats":
            return True

        elif self.current_filter_key == "Default view":

            # Show only main disable reasons
            if "disableReason" in stats_key and len(stats_value) == 0:
                if stats_key == "disableReason_idle" or stats_key == "disableReason_sleep":
                    return True
                else:
                    # Skipping empty disable reason
                    return False
            else:
                # Skipping fake stats
                if stats_key != "permissionsCheck":
                    return True
        else:
            # Show only values that match the filter
            return self.current_filter_key in stats_key

    def on_stats_selection_button_clicked(self, widget):
        """Called on any of the filter button clicks"""

        # we set the current stats key filter to the button's label
        self.current_filter_key = widget.get_label()

        # Update window title with the name of the filter selected.
        self.set_title("lightson-ng statistics: " + self.current_filter_key)

        log("%s stats key selected!" % self.current_filter_key)
        # we update the filter, which updates in turn the view

        self.key_filter.refilter()

    def on_key_press_event(self, widget, event):
        """ Close the window when Escape key pressed"""
        log("Key press on widget: " + Gdk.keyval_name(event.keyval))

        """
        # A little example.
        # check the event modifiers (can also use SHIFTMASK, etc.)
        ctrl = (event.state & Gdk.ModifierType.CONTROL_MASK)
        # see if we recognise a keypress
        if ctrl and event.keyval == Gdk.KEY_h:
            log("Ctrl+h pressed")
        """

        if event.keyval == Gdk.KEY_Escape:
            self.destroy_stats_dialog(widget)


class LightsonLogsWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="lightson-ng logs")
        self.set_border_width(10)
        self.set_default_size(1000, 500)

        self.grid = Gtk.Grid()
        self.add(self.grid)

        # create textview, text buffer.
        self.scrolledwindow = Gtk.ScrolledWindow()
        self.scrolledwindow.set_hexpand(True)
        self.scrolledwindow.set_vexpand(True)
        self.grid.attach(self.scrolledwindow, 0, 1, 3, 1)

        self.textview = Gtk.TextView()
        self.textbuffer = self.textview.get_buffer()
        self.textbuffer.set_text("If you see this text only, but no logs, then something went wrong in the log window.")
        self.scrolledwindow.add(self.textview)

        # Create some fancy tags to highlight text
        self.tag_bold = self.textbuffer.create_tag("bold", weight=Pango.Weight.BOLD)
        self.tag_italic = self.textbuffer.create_tag("italic", style=Pango.Style.ITALIC)
        self.tag_underline = self.textbuffer.create_tag("underline", underline=Pango.Underline.SINGLE)
        self.tag_found = self.textbuffer.create_tag("found", background="yellow")
        self.tag_regular = self.textbuffer.create_tag("regular")

        # self.fullscreen()
        self.maximize()
        self.show_all()
        # self.set_keep_above(True)
        self.present()

        # Define the tread, updating your text
        # Daemonize the thread to make it stop with the GUI
        self._quit_reading = None
        self.log_update = Thread(target=self.read_journal, daemon=True)
        # Start the thread
        self.log_update.start()
        # self.read_journal()
        self.connect('delete-event', self.on_log_win_close)

    # noinspection PyUnusedLocal
    def on_log_win_close(self, widget, arg2):
        """ Set the flag to quit reading the journal"""
        self._quit_reading = True

    def read_journal(self):
        """
        Continuously read logs of lightson-ng service and update log-window with new logs.
        Logs are shown in "tail -f" mode, i.e. window is automatically scrolled to the last log message.

        To make it happen with Gtk, text is updated in the idle_add() job and scrolling is done in
        a separate idle_add() job. Furthermore, read_journal() itself should not be called directly from
        Gtk, but from a separate thread.
        :return:
        """
        self._quit_reading = False

        # put lines read from the process into the queue
        # decode() - to convert from bytes to string
        def enqueue_output(out, queue):
            for q_line in out:
                queue.put(q_line.decode())
            out.close()

        journal_period = "today"
        # journal_period = "1 hour ago"
        # journal_period = "2 minutes ago"

        journal_ctl = subprocess.Popen(["journalctl", "--follow", "--identifier", "lightson-ng-stats",
                                        "--identifier", "lightson-ng",
                                        "--since", journal_period], stdout=subprocess.PIPE)
        queue_journal = Queue()
        thread_journal = Thread(target=enqueue_output, args=(journal_ctl.stdout, queue_journal))
        thread_journal.start()

        while True:

            # Properly terminate the thread
            if self._quit_reading:
                log("quit reading the journal")
                journal_ctl.terminate()
                journal_ctl.wait()
                break

            # read line without blocking
            try:
                # it does not work without timeout
                # line = q.get_nowait()
                line = queue_journal.get(timeout=.1)
            except Empty:
                # no output yet
                pass
            else:  # got line ... do something with line

                # Note: using GLib.PRIORITY_DEFAULT is a must, otherwise scrolling does not work
                # noinspection PyArgumentList
                GLib.idle_add(self.append_new_line, line, priority=GLib.PRIORITY_DEFAULT)
                GLib.idle_add(self.log_win_scroll_to_end)

    def append_new_line(self, line):
        """ Append new line to the end of text buffer """
        text_iter_end = self.textbuffer.get_end_iter()

        # Add some fancy tags and display the line
        # Highlight errors
        if "ERROR:" in line:

            tag = self.tag_found

        # Highlight the result of checks
        elif "Lights off..." in line\
                or "Disabling " in line\
                or "Enabling " in line:

            tag = self.tag_bold
            # log("Bold line: " + line)

        else:
            tag = self.tag_regular

        self.textbuffer.insert_with_tags(text_iter_end, line, tag)
        # self.textbuffer.insert(text_iter_end, line)

    def log_win_scroll_to_end(self):
        """ scroll the window to the end of text. """
        text_mark_end = self.textbuffer.create_mark("", self.textbuffer.get_end_iter(), False)
        self.textview.scroll_to_mark(text_mark_end, 0, False, 0, 0)


if __name__ == '__main__':
    # Parse command line
    cmdline = parse_command_line("lightson-ng-indicator - indicator GUI for lightson-ng")

    # A loop to handle both API and DBUS
    mainloop = GLib.MainLoop()

    LightsonIndicator()

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    Gtk.main()
