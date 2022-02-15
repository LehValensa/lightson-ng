#!/usr/bin/env python3

"""
 Statistics collection module. Helper for lightson-ng program.

 Copyright (c) 2022 grytsenko.alexander at gmail com
 URL: https://github.com/LehValensa/lightson-ng
 This script is licensed under GNU GPL version 2.0 or above

 Module:
   - creates DBUS service.
   - stores lightson statistics which is shown then by lightson-ng-indicator GUI program.
   - declares signals for IPC between lightson-ng process and lightson-ng-late-check service.
   - declares methods to communicate between lightson-ng, lightson-ng-indicator and the Late Check.
 This module is accomplished with *.conf file where DBUS permissions are configured.
 Python bindings of Gio and Glib used in the module are modern and powerful,
 but are documented purely, I've found only one working example:
    https://discourse.gnome.org/t/minimal-example-of-gdbus-in-python/3165/26
 Previous version of lightson-ng-stat was built on dbus-python library which is old, feature limited,
 not developing, but is working fine: https://dbus.freedesktop.org/doc/dbus-python/tutorial.html

 TODOFEATURE: seems, it is possible to configure DBUS permissions dynamically, at runtime, using apparmor_parser.
       to get a rid of statis lightson-ng-stat.conf file.
 TODOFEATURE: expose stats to dbus service properties, instead of internal variables.
                NoGo for pygobject yet because it is not ready yet, see confirmation here:
                https://stackoverflow.com/questions/52304262/writing-d-bus-service-with-pygobject
                while API exists:
                https://lazka.github.io/pgi-docs/#Gio-2.0/classes/DBusServer.html#Gio.DBusServer
                Tried, but no python examples on using it (while C -examples for gio-2.0 and glib-2.0 are compiling
                and working just fine, including GDBusObjectManagerServer)
 TODOFEATURE: check benefits installing ng-stat as standalone service into
              /usr/share/dbus-1/services/org.lightsOnStat.service:
              [D-BUS Service]
              Name=com.example.Sample
              Exec=/usr/local/bin/lightson-ng-stat.py
"""

from gi.repository import Gio, GLib
import re
from threading import Timer
import os
from argparse import ArgumentParser
import logging.handlers
import sys
import traceback


def get_dbus_config(var_name, default_value):
    """
    Get DBUS configuration from lightson-ng main shell module.
    lightson-ng should reside in the same directory as stats module
    :param var_name: name of variable
    :param default_value: set default value of variable, if not found in lightson-ng.
    :return:
    """
    try:
        ng_file = os.path.dirname(sys.argv[0]) + "/lightson-ng"
        with open(ng_file, "r") as file:
            for line in file:
                if re.search(var_name, line):
                    file.close()
                    return re.search(var_name + "=\"(.+)\"(.*)$", line).group(1)

    except (AttributeError, Exception):
        return default_value


# Try first the config exported from lightson-ng process, then use default values.
IF_NAME = get_dbus_config("LIGHTSON_STATS_INTERFACE", "org.LightsOn.StatInterface")
SRV_NAME = get_dbus_config("LIGHTSON_STATS_CONNECTION_NAME", "org.LightsOn.StatService")
OBJ_NAME = get_dbus_config("LIGHTSON_STATS_OBJECT", "/LightsOnStat")

# The name of lightson-ng service in systemd.
SYSTEMD_LIGHTSON_SERVICE = "lightson-ng.service"

# Timeout to wait until service is started by systemd.
SERVICE_OPERATION_TIMEOUT = 30

# Note: <property> is defined in XML, but not implemented yet.
serviceXml = (
        """<node>
          <interface name='""" + IF_NAME + """'>
          
        <!-- **************** methods -->
        <method name='SetStats'>
            <arg type='s' name='StatName' direction='in'>
                <doc:doc><doc:summary>Stats name</doc:summary></doc:doc>
            </arg>
            <arg type='s' name='StatValue' direction='in'>
                <doc:doc><doc:summary>Stats value</doc:summary></doc:doc>
            </arg>
            <doc:doc>
                <doc:description>
                    <doc:para>
                        Store the statistics gathered from lightson-ng service
                    </doc:para>
                </doc:description>
            </doc:doc>
        </method>

        <method name='GetStats'>
            <arg type='a{ss}' name='StatsAll' direction='out'/>
            <doc:doc>
                <doc:description>
                    <doc:para>
                        Retrieve the statistics gathered by lightson-ng
                    </doc:para>
                </doc:description>
            </doc:doc>
        </method>

        <method name='SetTimer'>
            <arg type='s' name='LoopDelay' direction='in'/>
            <doc:doc>
                <doc:description>
                    <doc:para>
                        Set timer to countdown the loop delay
                    </doc:para>
                </doc:description>
            </doc:doc>
        </method>
        
        <method name='PingStats'>
            <arg type='s' name='PingReply' direction='out'/>
            <doc:doc>
                <doc:description>
                    <doc:para>
                        Dummy method to check connection to the stats module
                    </doc:para>
                </doc:description>
            </doc:doc>
        </method>
        
        <method name='Quit'/>

        <!-- **************** signal emitters -->
        <method name='IterationFinished'/>
        <method name='ForceNewIteration'/>
        <method name='DoLateCheckIteration'/>
        <method name='AnyReasonFound'/>
        <method name='ReasonNotFound'/>
        <method name='DisableReasonFound'>
            <arg type='s' name='State' direction='in'/>
        </method>
        <method name='EnableReasonFound'>
            <arg type='s' name='State' direction='in'/>
        </method>

        <signal name='IterationFinishedSignal'>
            <doc:doc>
                <doc:description>
                    <doc:para>
                        Emitted when lightson's iteration is finished.
                    </doc:para>
                </doc:description>
            </doc:doc>
        </signal>
        
        <!-- signals enumerated -->
        <signal name='DoLateCheckSignal'/>
        <signal name='FinishLoopDelaySignal'/>
        <signal name='EnableReasonidleSignal'/>
        <signal name='DisableReasonidleSignal'/>
        <signal name='EnableReasonsleepSignal'/>
        <signal name='DisableReasonsleepSignal'/>

        <!-- **************** properties (unused yet) -->
        <!--
        <property name="disableReason_idle" type="s" access="read">
            <doc:doc>
                <doc:description>
                    <doc:para>
                      The reason of disabling idle state.
                    </doc:para>
                </doc:description>
            </doc:doc>
        </property>
        -->
          
      </interface>
    </node>"""
)


def _dictionary_to_string(source_dict):
    """
    Align the output with the DBUS method declaration - convert stat values to string,
    because dict members created inside in this class may have type other than string
    :param source_dict:
    :return:
    """
    dest_dict = {}
    for Key, Value in source_dict.items():
        dest_dict[Key] = str(Value)
    return dest_dict


def print_stats_array(source_dict):
    """
    Print the dict for debug purposes
    :param source_dict:
    :return:
    """
    for Key, Value in sorted(source_dict.items()):
        log("lightson-stats: " + Key + " =" + Value)


def _prepare_arguments(signature, prep_args):
    """
    Modify arguments returned by dbus method, to comply with Gio requirements
    :param signature: is the same as declared in XML, example: 'a{ss}' - if args is Python dictionary
    :param prep_args:
    :return:
    """
    return GLib.Variant("(%s)" % signature, prep_args)


def parse_command_line(description):
    """
    Parse command line options for lightson-stat module
    and also reuse this function in lightson-ng-indicator.
    Indicator GUI has some different defaults, so change them
    :param description:
    :return:
    """
    argument_parser = ArgumentParser(description=description)

    argument_parser.add_argument('-q', "--quiet", action="store_false", dest="print_stdout",
                                 help="don't print messages to stdout")
    argument_parser.add_argument('-s', "--no-syslog", action="store_false", dest="log_syslog",
                                 help="don't print messages to syslog")
    argument_parser.add_argument('-v', "--verbose", action="store_true", dest="verbose",
                                 help="print messages to syslog and to stdout")
    # noinspection PyGlobalUndefined
    global cmdline
    cmdline = argument_parser.parse_args()

    if cmdline.verbose:
        cmdline.print_stdout = True
        cmdline.log_syslog = True
    else:
        # Change defaults for lightson-ng-indicator
        if 'lightson-ng-indicator' in description:
            cmdline.print_stdout = False
            cmdline.log_syslog = False

    return cmdline


def log(message):
    """
    Log/print the message
    :param message:  text message to print
    :return:
    """

    # Setup new syslog logger if not done yet.
    if 'rootLogger' not in globals():
        # noinspection PyGlobalUndefined
        global rootLogger

        # The name of logger is a short filename without extension.
        rootLogger = logging.getLogger(os.path.basename(os.path.splitext(sys.argv[0])[0]))
        rootLogger.setLevel(logging.DEBUG)

        # Try the standard Unix device first, then try to go over UDP
        address = '/dev/log'
        if not os.path.exists(address):
            address = 'localhost', 514
        syslog_handler = logging.handlers.SysLogHandler(address)

        # Setup formatter the same way as /usr/bin/logger do.
        syslog_formatter = logging.Formatter('%(name)s[%(process)d]: %(message)s')
        syslog_handler.setFormatter(syslog_formatter)
        syslog_handler.setLevel(logging.INFO)

        rootLogger.addHandler(syslog_handler)

    if 'cmdline' not in globals():
        raise NameError("cmdline global variable not found. Forgot to parse arguments?")

    if cmdline.print_stdout:
        print(message)
    if cmdline.log_syslog:
        # noinspection PyUnboundLocalVariable
        rootLogger.info(message)


def log_error(message):
    log("ERROR: " + message)
    if cmdline.print_stdout:
        print(traceback.format_exc())


class StatObject:
    """
    DBUS helper for lightson-ng
    It can act as server for lightson-ng or as a client for lightson-ng-indicator.
    """

    def __init__(self):
        """
        Connect to dbus as service
        """

        self.statsOther = {}
        self.disableReason = {}
        self.checkPerformed = {}

        # Publish this service definition to DBUS.
        try:
            self.node_info = Gio.DBusNodeInfo.new_for_xml(serviceXml)
        except (ValueError, Exception):
            self.Quit()

        # First try to install the service in the system bus,
        # then, if launched not from root, fall back to the session bus.
        try:
            # non-root user should fall back to session bus.
            if os.getuid() != 0:
                raise

            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)

            self.owner_id = Gio.bus_own_name(
                Gio.BusType.SYSTEM,
                SRV_NAME,
                Gio.BusNameOwnerFlags.NONE,
                None, None, None)
            # ValueError is mentioned to prevent PyCharm's IDE complaints.
        except (ValueError, Exception):
            try:
                self._bus = Gio.bus_get_sync(Gio.BusType.SESSION)
                self.owner_id = Gio.bus_own_name(
                    Gio.BusType.SESSION,
                    SRV_NAME,
                    Gio.BusNameOwnerFlags.NONE,
                    None, None, None)
            except (ValueError, Exception):
                self.Quit()

        try:
            self.reg_id = self._bus.register_object(
                OBJ_NAME,
                self.node_info.interfaces[0],
                self.handle_method_call,
                None,
                None)
        except (ValueError, Exception):
            self.Quit()

    # noinspection PyPep8Naming
    def SetStats(self, params):
        """
        Get one statistics per call from lightson-ng process
        and store it in the corresponding array.
        Disable reasons and checks performed are stored in their own arrays.
        The rest types of stats go to the "stats" array
        :param params: array with the name and value of statistics variable.
        """
        statName = params.unpack()[0]
        statValue = params.unpack()[1]
        log(f"SetStats: Name={statName} Value={statValue}")

        if re.search(r'disableReason_', statName):

            self.disableReason[statName] = statValue

        elif re.search(r'checkPerformed_', statName):

            self.checkPerformed[statName] = statValue
        else:
            self.statsOther[statName] = statValue

    # noinspection PyUnusedLocal, PyPep8Naming
    def SetTimer(self, params):
        """
        Asynchronous timer. Used by lightson-ng to delay between iterations
        :param params: delay to set timer on
        """

        try:
            loopDelay = int(params.unpack()[0])
        except ValueError:
            log_error("ERROR: An integer value of loopDelay expected, got: " + str(params.unpack()[0]))
            return
        # FOR DEBUG ONLY:
        # loopDelay = int("3")
        log("Setting timer for " + str(loopDelay) + " seconds")

        t = Timer(loopDelay, self.emit_lightson_signal, args=("FinishLoopDelay",))
        t.start()

    def emit_lightson_signal(self, signal_name):
        """
        Emit a signal into the bus
        :param signal_name: signal name
        """
        new_signal_name = signal_name + "Signal"
        self._bus.emit_signal(None, OBJ_NAME, IF_NAME, new_signal_name, None)
        log("Signal: " + new_signal_name + " emitted")
        return

    # noinspection PyPep8Naming
    def GetStats(self):
        """
        Return the dictionary with statistics collected by lightson-ng.
        This method is called from lightson-ng-indicator
                Note: the only one of get* parameters should be set to True and will be returned
        :return: the dictionary wrapped into GLib.Variant, then into tuple to comply with Gio requirements.
                The output from "gdbus" looks like the dictionary inside the tuple,
                but d-feet shows ok: just the dictionary.
                The dictionary is a merge of disable reasons, checks and other stats.
        """

        returnStats = {**self.statsOther, **self.disableReason, **self.checkPerformed}

        print_stats_array(returnStats)

        return _prepare_arguments("a{ss}", (_dictionary_to_string(returnStats),))

    # noinspection PyPep8Naming
    def Quit(self):
        """
        Exit from the program. For debug purposes.
        """
        Gio.bus_unown_name(self.owner_id)
        mainloop.quit()
        log("Exiting stats")

    # noinspection PyUnusedLocal
    def handle_method_call(self, connection, sender, object_path, interface_name, method_name, params, invocation):
        """
        This is the top-level function that handles all the method calls to our server.
        The first four parameters are self-explanatory.
        `method_name` is a string that describes our method name.
        `params` is a GLib.Variant that are inputs/parameters to the method.
        `invocation` is a Gio.DBusMethodInvocation, something like a messenger that transports
        our reply to sender.
        For "params": we need to unpack GLib.Variant to a Python object. The unpacked one is always a tuple.
        Always return something (actually return value specified in XML) via invocation,
        otherwise client get a response-timeout error.
        """

        log(f"Handling method call: {method_name}")

        """
        ------------------- Signals
        Below are methods that emit signals corresponding to method's name.
        Note: sending signals directly from the client application is bad practice,
              that's why these methods are introduced.
        """
        if method_name == "ForceNewIteration":
            # Emit a signal to break the delay and loop over new iteration.
            # Can be issued manually from lightson-ng-indicator.
            self.emit_lightson_signal("FinishLoopDelay")
            invocation.return_value(None)

        elif method_name == "DoLateCheckIteration":
            # Emit a signal to break the delay and loop over new iteration specifically for Late Check service.
            # Within this iteration no inhibitors will be set.
            self.emit_lightson_signal(method_name)
            invocation.return_value(None)

        elif method_name == "IterationFinished":
            # Informational signal: lightson-ng just finished the iteration.
            self.emit_lightson_signal(method_name)
            invocation.return_value(None)

        elif method_name == "AnyReasonFound":
            # Informational signal: lightson has found some reason to inhibit Power Management state.
            self.emit_lightson_signal(method_name)
            invocation.return_value(None)

        elif method_name == "ReasonNotFound":
            # Informational signal: lightson has not found any reason to inhibit PM state.
            self.emit_lightson_signal(method_name)
            invocation.return_value(None)

        elif method_name == "DisableReasonFound":
            # Informational signal: lightson has found a reason to disable PM state.
            self.emit_lightson_signal("DisableReason"+str(params.unpack()[0]))
            invocation.return_value(None)

        elif method_name == "EnableReasonFound":
            # Informational signal: lightson has not found a reason to disable PM state.
            self.emit_lightson_signal("EnableReason"+str(params.unpack()[0]))
            invocation.return_value(None)

        # --------------------- Methods
        elif method_name == "SetTimer":
            self.SetTimer(params)
            invocation.return_value(None)

        elif method_name == "SetStats":
            self.SetStats(params)
            invocation.return_value(None)

        elif method_name == "GetStats":
            invocation.return_value(self.GetStats())

        elif method_name == "Quit":
            self.Quit()
            invocation.return_value(None)

        elif method_name == "PingStats":
            invocation.return_value(_prepare_arguments("s", ("Hello",)))

        else:
            invocation.return_error_literal(Gio.dbus_error_quark(), Gio.DBusError.UNKNOWN_METHOD,
                                            "No such method on interface: %s.%s" % (interface_name, method_name))


if __name__ == '__main__':

    # Parse command line
    parse_command_line(description="lightson-ng stats - dbus module")

    # A loop to handle both API and DBUS
    mainloop = GLib.MainLoop()

    # Stats DBUS service.
    StatObject()

    mainloop.run()
