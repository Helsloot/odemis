# -*- coding: utf-8 -*-
"""
Created on 6 April 2021

@author: Arthur Helsloot

Copyright © 2021 Arthur Helsloot, Delmic

This file is part of Odemis.

Odemis is free software: you can redistribute it and/or modify it under the terms
of the GNU General Public License version 2 as published by the Free Software
Foundation.

Odemis is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR
PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
Odemis. If not, see http://www.gnu.org/licenses/.
"""

from odemis import model
from odemis.model import isasync, CancellableThreadPoolExecutor, HwError
from odemis.util.weak import WeakMethod
from odemis.model._vattributes import NotSettableError
from ConsoleClient.Communication.Connection import Connection

import threading
import time
import logging
import inspect
from varname import nameof

COMPONENT_STOP = 1
COMPONENT_EMERGENCY_STOP = 2

VALVE_UNDEF = -1
VALVE_TRANSIT = 0
VALVE_OPEN = 1
VALVE_CLOSED = 2
VALVE_ERROR = 3

VACUUM_PRESSURE_RNG = (0, 110000)  # Pa
NITROGEN_PRESSURE_RNG = (0, 5000000)  # Pa  Eventhough 0 is nowhere near a realistic value for the compressed
# nitrogen, it is the initialisation value of this parameter in the Orsay server, meaning it needs to be included in
# the VA's range

ROD_NOT_DETECTED = 0
ROD_RESERVOIR_NOT_STRUCK = 1
ROD_OK = 2
ROD_READING_ERROR = 3

STR_OPEN = "OPEN"
STR_CLOSED = "CLOSED"
STR_PARK = "PARK"
STR_WORK = "WORK"

NO_ERROR_VALUES = (None, "", "None", "none", 0, "0", "NoError")

INTERLOCK_DETECTED_STR = "Interlock event detected"


class OrsayComponent(model.HwComponent):
    """
    This is an overarching component to represent the Orsay hardware
    """

    def __init__(self, name, role, children, host, daemon=None, **kwargs):
        """
        children (dict string->kwargs): parameters setting for the children.
            Known children are "pneumatic-suspension", "pressure", "pumping-system", "ups", "gis" and "gis-reservoir"
            They will be provided back in the .children VA
        host (string): ip address of the Orsay server
        Defines the following VA's and links them to the callbacks from the Orsay server:
        • processInfo (StringVA, read-only, value is datamodel.HybridPlatform.ProcessInfo.Actual)
        """

        model.HwComponent.__init__(self, name, role, daemon=daemon, **kwargs)

        self._host = host  # IP address of the Orsay server
        try:
            self._device = Connection(self._host)
        except Exception as ex:
            msg = "Failed to connect to Orsay server: %s. Check the network connection to the Orsay server." % str(ex)
            raise HwError(msg)
        time.sleep(1)  # allow for the connection to be made and the datamodel to be loaded
        self.datamodel = None

        self.processInfo = model.StringVA("", readonly=True)  # Contains a lot of information about the currently 
        # running process and a wide range thereof. For example it will show which valves are being closed and when 
        # the pumps are activated when setting the vacuum state to a new value. 

        self.on_connect()

        self._stop_connection_monitor = threading.Event()
        self._stop_connection_monitor.clear()
        self._connection_monitor_thread = threading.Thread(target=self._connection_monitor,
                                                           name="Orsay server connection monitor",
                                                           daemon=True)
        self._connection_monitor_thread.start()

        # create the pneumatic suspension child
        try:
            kwargs = children["pneumatic-suspension"]
        except (KeyError, TypeError):
            logging.info("Orsay was not given a 'pneumatic-suspension' child")
        else:
            self._pneumaticSuspension = pneumaticSuspension(parent=self, daemon=daemon, **kwargs)
            self.children.value.add(self._pneumaticSuspension)

        # create the pressure child for the chamber
        try:
            kwargs = children["pressure"]
        except (KeyError, TypeError):
            logging.info("Orsay was not given a 'pressure' child")
        else:
            self._pressure = vacuumChamber(parent=self, daemon=daemon, **kwargs)
            self.children.value.add(self._pressure)

        # create the pumping system child
        try:
            kwargs = children["pumping-system"]
        except (KeyError, TypeError):
            logging.info("Orsay was not given a 'pumping-system' child")
        else:
            self._pumpingSystem = pumpingSystem(parent=self, daemon=daemon, **kwargs)
            self.children.value.add(self._pumpingSystem)

        # create the UPS child
        try:
            kwargs = children["ups"]
        except (KeyError, TypeError):
            logging.info("Orsay was not given a 'ups' child")
        else:
            self._ups = UPS(parent=self, daemon=daemon, **kwargs)
            self.children.value.add(self._ups)

        # create the GIS child
        try:
            kwargs = children["gis"]
        except (KeyError, TypeError):
            logging.info("Orsay was not given a 'gis' child")
        else:
            self._gis = GIS(parent=self, daemon=daemon, **kwargs)
            self.children.value.add(self._gis)

        # create the GIS Reservoir child
        try:
            kwargs = children["gis-reservoir"]
        except (KeyError, TypeError):
            logging.info("Orsay was not given a 'gis-reservoir' child")
        else:
            self._gis_reservoir = GISReservoir(parent=self, daemon=daemon, **kwargs)
            self.children.value.add(self._gis_reservoir)

        # create the test child
        try:
            kwargs = children["test"]
        except (KeyError, TypeError):
            logging.info("Orsay was not given a 'test' child")
        else:
            self._test_device = TestDevice(parent=self, daemon=daemon, **kwargs)
            self.children.value.add(self._test_device)

        # create the FIB source child
        try:
            kwargs = children["fib-source"]
        except (KeyError, TypeError):
            logging.info("Orsay was not given a 'fib-source' child")
        else:
            self._fib_source = FIBSource(parent=self, daemon=daemon, **kwargs)
            self.children.value.add(self._fib_source)

    def on_connect(self):
        """
        Defines direct pointers to server components and connects parameter callbacks for the Orsay server.
        Needs to be called after connection and reconnection to the server.
        """
        self.datamodel = self._device.datamodel
        self.datamodel.HybridPlatform.ProcessInfo.Subscribe(self._updateProcessInfo)

        self.update_VAs()

    def update_VAs(self):
        """
        Update the VA's. Should be called after reconnection to the server
        """
        self._updateProcessInfo()

    def _connection_monitor(self):
        """
        Once in a while, check the connection to the Orsay server, reconnect if needed and update all VA's
        """
        try:
            while not self._stop_connection_monitor.is_set():

                if self._device and (self._device.HttpConnection._HTTPConnection__response is None or
                                     self._device.MessageConnection.Connection._HTTPConnection__response is None):
                    self.state._set_value(HwError("Connection to Orsay server lost. Trying to reconnect..."),
                                          force_write=True)
                    self._device.HttpConnection.close()  # close the current connection
                    self._device.MessageConnection.Connection.close()
                    self._device = None  # destroy the current connection object

                if not self._device:  # if there currently is no connection
                    try:  # try to reconnect
                        self._device = Connection(self._host)
                        time.sleep(1)
                        self.on_connect()
                        for child in self.children.value:
                            child.on_connect()
                        self.state._set_value(model.ST_RUNNING, force_write=True)
                    except Exception:
                        logging.exception("Trying to reconnect to Orsay server.")
                else:
                    try:
                        self.update_VAs()
                        for child in self.children.value:
                            child.update_VAs()
                    except Exception:
                        logging.exception("Failure while updating VAs.")
                self._stop_connection_monitor.wait(5)
        except Exception:
            logging.exception("Failure in connection monitor thread.")
        finally:
            logging.debug("Orsay server connection monitor thread finished.")
            self._stop_connection_monitor.clear()

    def _updateProcessInfo(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the process information from the Orsay server and saves it in the processInfo VA
        """
        if parameter is None:
            parameter = self.datamodel.HybridPlatform.ProcessInfo
        if parameter is not self.datamodel.HybridPlatform.ProcessInfo:
            raise ValueError("Incorrect parameter passed to _updateProcessInfo. Parameter should be "
                             "datamodel.HybridPlatform.ProcessInfo. Parameter passed is %s"
                             % parameter.Name)
        if attributeName != "Actual":
            return
        currentProcessInfo = str(parameter.Actual)
        currentProcessInfo = currentProcessInfo.replace("N/A", "")
        logging.debug("ProcessInfo update: " + currentProcessInfo)
        self.processInfo._set_value(currentProcessInfo, force_write=True)

    def terminate(self):
        """
        Called when Odemis is closed
        """
        if self._device:
            self.datamodel.HybridPlatform.ProcessInfo.Unsubscribe(self._updateProcessInfo)
            if self._pneumaticSuspension:
                self._pneumaticSuspension.terminate()
                self._pneumaticSuspension = None
            if self._pressure:
                self._pressure.terminate()
                self._pressure = None
            if self._pumpingSystem:
                self._pumpingSystem.terminate()
                self._pumpingSystem = None
            if self._ups:
                self._ups.terminate()
                self._ups = None
            if self._gis:
                self._gis.terminate()
                self._gis = None
            if self._gis_reservoir:
                self._gis_reservoir.terminate()
                self._gis_reservoir = None
            super(OrsayComponent, self).terminate()
            self._stop_connection_monitor.set()  # stop trying to reconnect
            self._device.HttpConnection.close()  # close the connection
            self._device.MessageConnection.Connection.close()
            self.datamodel = None
            self._device = None


class pneumaticSuspension(model.HwComponent):
    """
    This represents the Pneumatic Suspension from Orsay Physics
    """

    def __init__(self, name, role, parent, **kwargs):
        """
        Defines the following VA's and links them to the callbacks from the Orsay server:
        • power (BooleanVA, value corresponds to _valve.Actual == VALVE_OPEN, set to True to open/start and False to
        close/stop)
        • pressure (FloatContinuous, range=NITROGEN_PRESSURE_RNG, read-only, unit is "Pa", value is _gauge.Actual)
        """

        # we will fill the set of children with Components later in ._children
        model.HwComponent.__init__(self, name, role, parent=parent, **kwargs)

        self._valve = None
        self._gauge = None

        self.pressure = model.FloatContinuous(NITROGEN_PRESSURE_RNG[0], range=NITROGEN_PRESSURE_RNG,
                                              readonly=True, unit="Pa")
        self.power = model.BooleanVA(False, setter=self._changeValve)

        self.on_connect()

    def on_connect(self):
        """
        Defines direct pointers to server components and connects parameter callbacks for the Orsay server.
        Needs to be called after connection and reconnection to the server.
        """
        self._valve = self.parent.datamodel.HybridPlatform.ValvePneumaticSuspension.IsOpen
        self._gauge = self.parent.datamodel.HybridPlatform.Manometer2.Pressure

        self.parent.datamodel.HybridPlatform.ValvePneumaticSuspension.ErrorState.Subscribe(self._updateErrorState)
        self.parent.datamodel.HybridPlatform.Manometer2.ErrorState.Subscribe(self._updateErrorState)
        self._valve.Subscribe(self._updatePower)
        self._gauge.Subscribe(self._updatePressure)

        self.update_VAs()

    def update_VAs(self):
        """
        Update the VA's. Should be called after reconnection to the server
        """
        self._updateErrorState()
        self._updatePower()
        self._updatePressure()

    def _updatePower(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the power status from the Orsay server and saves it in the power VA
        """
        if parameter is None:
            parameter = self._valve
        if parameter is not self._valve:
            raise ValueError("Incorrect parameter passed to _updatePower. Parameter should be "
                             "datamodel.HybridPlatform.ValvePneumaticSuspension.IsOpen. Parameter passed is %s"
                             % parameter.Name)
        if attributeName != "Actual":
            return
        valve_state = int(parameter.Actual)
        log_msg = "ValvePneumaticSuspension state changed to: %s."
        if valve_state in (VALVE_UNDEF, VALVE_ERROR):
            logging.warning(log_msg % valve_state)
            self._updateErrorState()
        elif valve_state in (VALVE_OPEN, VALVE_CLOSED):
            logging.debug(log_msg % valve_state)
            new_value = valve_state == VALVE_OPEN
            self.power._value = new_value  # to not call the setter
            self.power.notify(new_value)
        else:  # if _valve.Actual == VALVE_TRANSIT, or undefined
            logging.debug(log_msg % valve_state)

    def _updatePressure(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the pressure from the Orsay server and saves it in the pressure VA
        """
        if parameter is None:
            parameter = self._gauge
        if parameter is not self._gauge:
            raise ValueError("Incorrect parameter passed to _updatePressure. Parameter should be "
                             "datamodel.HybridPlatform.Manometer2.Pressure. Parameter passed is %s"
                             % parameter.Name)
        if attributeName != "Actual":
            return
        self.pressure._set_value(float(parameter.Actual), force_write=True)

    def _updateErrorState(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the error state from the Orsay server and saves it in the state VA
        """
        if parameter is not self.parent.datamodel.HybridPlatform.ValvePneumaticSuspension.ErrorState and parameter is \
                not self.parent.datamodel.HybridPlatform.Manometer2.ErrorState and parameter is not None:
            raise ValueError("Incorrect parameter passed to _updateErrorState. Parameter should be "
                             "datamodel.HybridPlatform.ValvePneumaticSuspension.ErrorState or "
                             "datamodel.HybridPlatform.Manometer2.ErrorState or None. Parameter passed is %s"
                             % parameter.Name)
        if attributeName != "Actual":
            return
        eState = ""
        vpsEState = str(self.parent.datamodel.HybridPlatform.ValvePneumaticSuspension.ErrorState.Actual)
        manEState = str(self.parent.datamodel.HybridPlatform.Manometer2.ErrorState.Actual)
        if vpsEState not in NO_ERROR_VALUES:
            eState += "ValvePneumaticSuspension error: " + vpsEState
        if manEState not in NO_ERROR_VALUES:
            if not eState == "":
                eState += ", "
            eState += "Manometer2 error: " + manEState
        valve_state = int(self._valve.Actual)
        if valve_state == VALVE_ERROR:  # in case of valve error
            if not eState == "":
                eState += ", "
            eState += "ValvePneumaticSuspension is in error"
        elif valve_state == VALVE_UNDEF:  # in case no communication is present with the valve
            if not eState == "":
                eState += ", "
            eState += "ValvePneumaticSuspension could not be contacted"
        if eState == "":
            self.state._set_value(model.ST_RUNNING, force_write=True)
        else:
            self.state._set_value(HwError(eState), force_write=True)

    def _changeValve(self, goal):
        """
        goal (bool): goal position of the valve: (True: "open", False: "closed")
        return (bool): goal position of the valve set to the server: (True: "open", False: "closed")

        Opens or closes the valve.
        Returns True if the valve is opened, False otherwise
        """
        logging.debug("Setting valve to %s." % goal)
        self._valve.Target = VALVE_OPEN if goal else VALVE_CLOSED
        return self._valve.Target == VALVE_OPEN

    def terminate(self):
        """
        Called when Odemis is closed
        """
        if self._gauge:
            self.parent.datamodel.HybridPlatform.ValvePneumaticSuspension.ErrorState.Unsubscribe(self._updateErrorState)
            self.parent.datamodel.HybridPlatform.Manometer2.ErrorState.Unsubscribe(self._updateErrorState)
            self._valve.Unsubscribe(self._updatePower)
            self._gauge.Unsubscribe(self._updatePressure)
            self._valve = None
            self._gauge = None


class vacuumChamber(model.Actuator):
    """
    This represents the vacuum chamber from Orsay Physics
    """

    def __init__(self, name, role, parent, **kwargs):
        """
        Has axes:
        • "vacuum": choices is {0 : "vented", 1 : "primary vacuum", 2 : "high vacuum"}

        Defines the following VA's and links them to the callbacks from the Orsay server:
        • gateOpen (BooleanVA, set to True to open/start and False to close/stop)
        • position (VA, read-only, value is {"vacuum" : _chamber.VacuumStatus.Actual})
        • pressure (FloatContinuous, range=VACUUM_PRESSURE_RNG, read-only, unit is "Pa",
                    value is _chamber.Pressure.Actual)
        """

        axes = {"vacuum": model.Axis(unit=None, choices={0: "vented", 1: "primary vacuum", 2: "high vacuum"})}

        model.Actuator.__init__(self, name, role, parent=parent, axes=axes, **kwargs)

        self._gate = None
        self._chamber = None

        self.pressure = model.FloatContinuous(VACUUM_PRESSURE_RNG[0], range=VACUUM_PRESSURE_RNG,
                                              readonly=True, unit="Pa")
        self.gateOpen = model.BooleanVA(False, setter=self._changeGateOpen)
        self.position = model.VigilantAttribute({"vacuum": 0}, readonly=True)

        self._vacuumStatusReached = threading.Event()
        self._vacuumStatusReached.set()

        self.on_connect()

        self._executor = CancellableThreadPoolExecutor(max_workers=1)

    def on_connect(self):
        """
        Defines direct pointers to server components and connects parameter callbacks for the Orsay server.
        Needs to be called after connection and reconnection to the server.
        """
        self._gate = self.parent.datamodel.HybridPlatform.ValveP5
        self._chamber = self.parent.datamodel.HybridPlatform.AnalysisChamber

        self._gate.ErrorState.Subscribe(self._updateErrorState)
        self._chamber.VacuumStatus.Subscribe(self._updatePosition)
        self._chamber.Pressure.Subscribe(self._updatePressure)
        self._gate.IsOpen.Subscribe(self._updateGateOpen)

        self.update_VAs()

    def update_VAs(self):
        """
        Update the VA's. Should be called after reconnection to the server
        """
        self._updateErrorState()
        self._updatePosition()
        self._updatePressure()
        self._updateGateOpen()

    def _updateErrorState(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the error state from the Orsay server and saves it in the state VA
        """
        if parameter is not self._gate.ErrorState and parameter is not None:
            raise ValueError("Incorrect parameter passed to _updateErrorState. Parameter should be "
                             "datamodel.HybridPlatform.ValveP5.ErrorState or None. Parameter passed is %s"
                             % parameter.Name)
        if attributeName != "Actual":
            return
        eState = ""
        gateEState = self._gate.ErrorState.Actual
        if gateEState not in NO_ERROR_VALUES:
            eState += "ValveP5 error: " + gateEState
        valve_state = int(self._gate.IsOpen.Actual)
        if valve_state == VALVE_ERROR:  # in case of valve error
            if not eState == "":
                eState += ", "
            eState += "ValveP5 is in error"
        elif valve_state == VALVE_UNDEF:  # in case no communication is present with the valve
            if not eState == "":
                eState += ", "
            eState += "ValveP5 could not be contacted"
        if eState == "":
            self.state._set_value(model.ST_RUNNING, force_write=True)
        else:
            self.state._set_value(HwError(eState), force_write=True)

    def _updateGateOpen(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads if ValveP5 is open from the Orsay server and saves it in the gateOpen VA
        """
        if parameter is None:
            parameter = self._gate.IsOpen
        if parameter is not self._gate.IsOpen:
            raise ValueError("Incorrect parameter passed to _updateGateOpen. Parameter should be "
                             "datamodel.HybridPlatform.ValveP5.IsOpen. Parameter passed is %s"
                             % parameter.Name)
        if attributeName != "Actual":
            return
        valve_state = int(parameter.Actual)
        log_msg = "ValveP5 state is: %s."
        if valve_state in (VALVE_UNDEF, VALVE_ERROR):
            logging.warning(log_msg % valve_state)
            self._updateErrorState()
        elif valve_state in (VALVE_OPEN, VALVE_CLOSED):
            new_value = valve_state == VALVE_OPEN
            if not new_value == self.gateOpen.value:
                logging.debug(log_msg % valve_state)
            self.gateOpen._value = new_value  # to not call the setter
            self.gateOpen.notify(new_value)
        else:  # if parameter.Actual is VALVE_TRANSIT, or undefined
            logging.debug(log_msg % valve_state)

    def _updatePosition(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the vacuum state from the Orsay server and saves it in the position VA
        """
        if parameter is None:
            parameter = self._chamber.VacuumStatus
        if parameter is not self._chamber.VacuumStatus:
            raise ValueError("Incorrect parameter passed to _updatePosition. Parameter should be "
                             "datamodel.HybridPlatform.AnalysisChamber.VacuumStatus. Parameter passed is %s"
                             % parameter.Name)
        if parameter.Actual == parameter.Target:
            logging.debug("Target vacuum state reached.")
            self._vacuumStatusReached.set()
        else:
            self._vacuumStatusReached.clear()
        if attributeName != "Actual":
            return
        currentVacuum = int(parameter.Actual)
        logging.debug("Vacuum status changed to %f." % currentVacuum)
        self.position._set_value({"vacuum": currentVacuum}, force_write=True)

    def _updatePressure(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the chamber pressure from the Orsay server and saves it in the pressure VA
        """
        if parameter is None:
            parameter = self._chamber.Pressure
        if parameter is not self._chamber.Pressure:
            raise ValueError("Incorrect parameter passed to _updatePressure. Parameter should be "
                             "datamodel.HybridPlatform.AnalysisChamber.Pressure. Parameter passed is %s"
                             % parameter.Name)
        if attributeName != "Actual":
            return
        self.pressure._set_value(float(parameter.Actual), force_write=True)

    def _changeVacuum(self, goal):
        """
        goal (int): goal state of the vacuum: (0: "vented", 1: "primary vacuum", 2: "high vacuum")
        return (int): actual state of the vacuum at the end of this function: (0: "vented", 1: "primary vacuum",
                      2: "high vacuum")

        Sets the vacuum status on the Orsay server to argument goal and waits until it is reached.
        Then returns the reached vacuum status.
        """
        logging.debug("Setting vacuum status to %s." % self.axes["vacuum"].choices[goal])
        self._vacuumStatusReached.clear()  # to make sure it will wait
        self._chamber.VacuumStatus.Target = goal
        if not self._vacuumStatusReached.wait(18000):  # wait maximally 5 hours
            raise TimeoutError("Something went wrong awaiting a change in the vacuum status.")
        self._updatePosition()

    def _changeGateOpen(self, goal):
        """
        goal (bool): goal position of the gate: (True: "open", False: "closed")
        return (bool): goal position of the gate as set to the server: (True: "open", False: "closed")

        Opens ValveP5 on the Orsay server if argument goal is True. Closes it otherwise.
        """
        logging.debug("Setting gate to %s." % ("open" if goal else "closed"))
        self._gate.IsOpen.Target = VALVE_OPEN if goal else VALVE_CLOSED
        return self._gate.IsOpen.Target == VALVE_OPEN

    @isasync
    def moveAbs(self, pos):
        """
        Move the axis of this actuator to pos.
        """
        self._checkMoveAbs(pos)
        return self._executor.submit(self._changeVacuum, goal=pos["vacuum"])

    @isasync
    def moveRel(self, shift):
        """
        Move the axis of this actuator by shift.
        """
        raise NotImplementedError("Relative movements are not implemented for vacuum control. Use moveAbs instead.")

    def stop(self, axes=None):
        """
        Stop changing the vacuum status
        """
        if not axes or "vacuum" in axes:
            logging.debug("Stopping vacuum.")
            self.parent.datamodel.HybridPlatform.Stop.Target = COMPONENT_STOP
            self.parent.datamodel.HybridPlatform.Cancel.Target = True  # tell the server to stop what it's doing
            self._changeVacuum(int(self._chamber.VacuumStatus.Actual))  # the current target is the current state and
            # wait. This assures the executor does not infinitely wait until VacuumStatus.Actual equals
            # VacuumStatus.Target
            self.parent.datamodel.HybridPlatform.Stop.Target = COMPONENT_STOP
            self.parent.datamodel.HybridPlatform.Cancel.Target = True  # tell the server to stop what it's doing again
            self._executor.cancel()

    def terminate(self):
        """
        Called when Odemis is closed
        """
        if self._chamber:
            self._gate.ErrorState.Unsubscribe(self._updateErrorState)
            self._chamber.VacuumStatus.Unsubscribe(self._updatePosition)
            self._chamber.Pressure.Unsubscribe(self._updatePressure)
            self._gate.IsOpen.Unsubscribe(self._updateGateOpen)
            if self._executor:
                self._executor.shutdown()
                self._executor = None
            self._gate = None
            self._chamber = None


class pumpingSystem(model.HwComponent):
    """
    This represents the pumping system from Orsay Physics
    """

    def __init__(self, name, role, parent, **kwargs):
        """
        Defines the following VA's and links them to the callbacks from the Orsay server:
        • speed (FloatVA, read-only, unit is "Hz", value is _system.TurboPump1.Speed.Actual)
        • temperature (FloatVA, read-only, unit is "°C", value is _system.TurboPump1.Temperature.Actual)
        • power (FloatVA, read-only, unit is "W", value is _system.TurboPump1.Power.Actual)
        • speedReached (BooleanVA, read-only, value is _system.TurboPump1.SpeedReached.Actual)
        • turboPumpOn (BooleanVA, read-only, value is _system.TurboPump1.IsOn.Actual)
        • primaryPumpOn (BooleanVA, read-only, value is parent.datamodel.HybridPlatform.PrimaryPumpState.Actual)
        • nitrogenPressure (FloatVA, read-only, unit is "Pa", value is _system.Manometer1.Pressure.Actual)
        """

        model.HwComponent.__init__(self, name, role, parent=parent, **kwargs)

        self._system = None

        self.speed = model.FloatVA(0.0, readonly=True, unit="Hz")
        self.temperature = model.FloatVA(0.0, readonly=True, unit="°C")
        self.power = model.FloatVA(0.0, readonly=True, unit="W")
        self.speedReached = model.BooleanVA(False, readonly=True)
        self.turboPumpOn = model.BooleanVA(False, readonly=True)
        self.primaryPumpOn = model.BooleanVA(False, readonly=True)
        self.nitrogenPressure = model.FloatVA(0.0, readonly=True, unit="Pa")

        self.on_connect()

    def on_connect(self):
        """
        Defines direct pointers to server components and connects parameter callbacks for the Orsay server.
        Needs to be called after connection and reconnection to the server.
        """
        self._system = self.parent.datamodel.HybridPlatform.PumpingSystem

        self._system.Manometer1.ErrorState.Subscribe(self._updateErrorState)
        self._system.TurboPump1.ErrorState.Subscribe(self._updateErrorState)
        self._system.TurboPump1.Speed.Subscribe(self._updateSpeed)
        self._system.TurboPump1.Temperature.Subscribe(self._updateTemperature)
        self._system.TurboPump1.Power.Subscribe(self._updatePower)
        self._system.TurboPump1.SpeedReached.Subscribe(self._updateSpeedReached)
        self._system.TurboPump1.IsOn.Subscribe(self._updateTurboPumpOn)
        self.parent.datamodel.HybridPlatform.PrimaryPumpState.Subscribe(self._updatePrimaryPumpOn)
        self._system.Manometer1.Pressure.Subscribe(self._updateNitrogenPressure)

        self.update_VAs()

    def update_VAs(self):
        """
        Update the VA's. Should be called after reconnection to the server
        """
        self._updateErrorState()
        self._updateSpeed()
        self._updateTemperature()
        self._updatePower()
        self._updateSpeedReached()
        self._updateTurboPumpOn()
        self._updatePrimaryPumpOn()
        self._updateNitrogenPressure()

    def _updateErrorState(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the error state from the Orsay server and saves it in the state VA
        """
        if parameter is not self._system.Manometer1.ErrorState and parameter is not self._system.TurboPump1.ErrorState \
                and parameter is not None:
            raise ValueError("Incorrect parameter passed to _updateErrorState. Parameter should be "
                             "datamodel.HybridPlatform.PumpingSystem.Manometer1.ErrorState or "
                             "datamodel.HybridPlatform.PumpingSystem.TurboPump1.ErrorState or None. "
                             "Parameter passed is %s" % parameter.Name)
        if attributeName != "Actual":
            return
        eState = ""
        manEState = self._system.Manometer1.ErrorState.Actual
        tpEState = self._system.TurboPump1.ErrorState.Actual
        if manEState not in NO_ERROR_VALUES:
            eState += "Manometer1 error: " + manEState
        if tpEState not in NO_ERROR_VALUES:
            if not eState == "":
                eState += ", "
            eState += "TurboPump1 error: " + tpEState
        if eState == "":
            self.state._set_value(model.ST_RUNNING, force_write=True)
        else:
            self.state._set_value(HwError(eState), force_write=True)

    def _updateSpeed(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the turbopump's speed from the Orsay server and saves it in the speed VA
        """
        if parameter is None:
            parameter = self._system.TurboPump1.Speed
        if parameter is not self._system.TurboPump1.Speed:
            raise ValueError("Incorrect parameter passed to _updateSpeed. Parameter should be "
                             "datamodel.HybridPlatform.PumpingSystem.TurboPump1.Speed. Parameter passed is %s"
                             % parameter.Name)
        if attributeName != "Actual":
            return
        self.speed._set_value(float(parameter.Actual), force_write=True)

    def _updateTemperature(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the turbopump's temperature from the Orsay server and saves it in the temperature VA
        """
        if parameter is None:
            parameter = self._system.TurboPump1.Temperature
        if parameter is not self._system.TurboPump1.Temperature:
            raise ValueError("Incorrect parameter passed to _updateTemperature. Parameter should be "
                             "datamodel.HybridPlatform.PumpingSystem.TurboPump1.Temperature. Parameter passed is %s"
                             % parameter.Name)
        if attributeName != "Actual":
            return
        self.temperature._set_value(float(self._system.TurboPump1.Temperature.Actual), force_write=True)

    def _updatePower(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the turbopump's power from the Orsay server and saves it in the power VA
        """
        if parameter is None:
            parameter = self._system.TurboPump1.Power
        if parameter is not self._system.TurboPump1.Power:
            raise ValueError("Incorrect parameter passed to _updatePower. Parameter should be "
                             "datamodel.HybridPlatform.PumpingSystem.TurboPump1.Power. Parameter passed is %s"
                             % parameter.Name)
        if attributeName != "Actual":
            return
        self.power._set_value(float(parameter.Actual), force_write=True)

    def _updateSpeedReached(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads if the turbopump has reached its maximum speed from the Orsay server and saves it in the speedReached VA
        """
        if parameter is None:
            parameter = self._system.TurboPump1.SpeedReached
        if parameter is not self._system.TurboPump1.SpeedReached:
            raise ValueError("Incorrect parameter passed to _updateSpeedReached. Parameter should be "
                             "datamodel.HybridPlatform.PumpingSystem.TurboPump1.SpeedReached. Parameter passed is %s"
                             % parameter.Name)
        if attributeName != "Actual":
            return
        logging.debug("Speed reached changed to %s." % str(parameter.Actual))
        self.speedReached._set_value(str(parameter.Actual).lower() == "true", force_write=True)

    def _updateTurboPumpOn(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads if the turbopump is currently on from the Orsay server and saves it in the turboPumpOn VA
        """
        if parameter is None:
            parameter = self._system.TurboPump1.IsOn
        if parameter is not self._system.TurboPump1.IsOn:
            raise ValueError("Incorrect parameter passed to _updateTurboPumpOn. Parameter should be "
                             "datamodel.HybridPlatform.PumpingSystem.TurboPump1.IsOn. Parameter passed is %s"
                             % parameter.Name)
        if attributeName != "Actual":
            return
        state = str(parameter.Actual).lower() == "true"
        logging.debug("Turbopump turned %s." % ("on" if state else "off"))
        self.turboPumpOn._set_value(state, force_write=True)

    def _updatePrimaryPumpOn(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads if the primary pump is currently on from the Orsay server and saves it in the primaryPumpOn VA
        """
        if parameter is None:
            parameter = self.parent.datamodel.HybridPlatform.PrimaryPumpState
        if parameter is not self.parent.datamodel.HybridPlatform.PrimaryPumpState:
            raise ValueError("Incorrect parameter passed to _updatePrimaryPumpOn. Parameter should be "
                             "datamodel.HybridPlatform.PrimaryPumpState. Parameter passed is %s"
                             % parameter.Name)
        if attributeName != "Actual":
            return
        state = str(parameter.Actual).lower() == "true"
        logging.debug("Primary pump turned %s." % ("on" if state else "off"))
        self.primaryPumpOn._set_value(state, force_write=True)

    def _updateNitrogenPressure(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads pressure on nitrogen inlet to the turbopump from the Orsay server and saves it in the nitrogenPressure VA
        """
        if parameter is None:
            parameter = self._system.Manometer1.Pressure
        if parameter is not self._system.Manometer1.Pressure:
            raise ValueError("Incorrect parameter passed to _updateNitrogenPressure. Parameter should be "
                             "datamodel.HybridPlatform.PumpingSystem.Manometer1.Pressure. Parameter passed is %s"
                             % parameter.Name)
        if attributeName != "Actual":
            return
        self.nitrogenPressure._set_value(float(parameter.Actual), force_write=True)

    def terminate(self):
        """
        Called when Odemis is closed
        """
        if self._system:
            self._system.Manometer1.ErrorState.Unsubscribe(self._updateErrorState)
            self._system.TurboPump1.ErrorState.Unsubscribe(self._updateErrorState)
            self._system.TurboPump1.Speed.Unsubscribe(self._updateSpeed)
            self._system.TurboPump1.Temperature.Unsubscribe(self._updateTemperature)
            self._system.TurboPump1.Power.Unsubscribe(self._updatePower)
            self._system.TurboPump1.SpeedReached.Unsubscribe(self._updateSpeedReached)
            self._system.TurboPump1.IsOn.Unsubscribe(self._updateTurboPumpOn)
            self.parent.datamodel.HybridPlatform.PrimaryPumpState.Unsubscribe(self._updatePrimaryPumpOn)
            self._system.Manometer1.Pressure.Unsubscribe(self._updateNitrogenPressure)
            self._system = None


class UPS(model.HwComponent):
    """
    This represents the uniterupted power supply from Orsay Physics
    """

    def __init__(self, name, role, parent, **kwargs):
        """
        Defines the following VA's and links them to the callbacks from the Orsay server:
        • level (FloatContinuous, range=(0.0, 1.0), read-only, value represents the fraction of full charge of the UPS)
        """

        model.HwComponent.__init__(self, name, role, parent=parent, **kwargs)

        self._blevel = None

        self.level = model.FloatContinuous(1.0, range=(0.0, 1.0), readonly=True, unit="")

        self.on_connect()

    def on_connect(self):
        """
        Defines direct pointers to server components and connects parameter callbacks for the Orsay server.
        Needs to be called after connection and reconnection to the server.
        """
        self._blevel = self.parent.datamodel.HybridPlatform.UPS.UPScontroller.BatteryLevel

        self._blevel.Subscribe(self._updateLevel)

        self.update_VAs()

    def update_VAs(self):
        """
        Update the VA's. Should be called after reconnection to the server
        """
        self._updateLevel()

    def _updateLevel(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the battery level of the UPS from the Orsay server and saves it in the level VA
        """
        if parameter is None:
            parameter = self._blevel
        if parameter is not self._blevel:
            raise ValueError("Incorrect parameter passed to _updateLevel. Parameter should be "
                             "datamodel.HybridPlatform.UPS.UPScontroller.BatteryLevel")
        if attributeName != "Actual":
            return
        currentLevel = float(parameter.Actual)
        self.level._set_value(currentLevel / 100, force_write=True)

    def terminate(self):
        """
        Called when Odemis is closed
        """
        if self._blevel:
            self._blevel.Unsubscribe(self._updateLevel)
            self._blevel = None


class GIS(model.Actuator):
    """
    This represents the Gas Injection Sytem (GIS) from Orsay Physics
    """

    def __init__(self, name, role, parent, **kwargs):
        """
        Has axes:
        • "arm": unit is "None", choices is {True: "engaged", False: "parked"}

        Defines the following VA's and links them to the callbacks from the Orsay server:
        • position (VA, read-only, value is {"arm" : _positionPar.Actual})
        • injectingGas (BooleanVA, set to True to open/start the gas flow and False to close/stop the gas flow)
        """
        axes = {"arm": model.Axis(unit=None, choices={True: "engaged", False: "parked"})}

        model.Actuator.__init__(self, name, role, parent=parent, axes=axes, **kwargs)

        self._gis = None
        self._errorPar = None
        self._positionPar = None
        self._reservoirPar = None

        self._armPositionReached = threading.Event()
        self._armPositionReached.set()

        self.position = model.VigilantAttribute({"arm": False}, readonly=True)
        self.injectingGas = model.BooleanVA(False, setter=self._setInjectingGas)

        self.on_connect()

        self._executor = CancellableThreadPoolExecutor(max_workers=1)

    def on_connect(self):
        """
        Defines direct pointers to server components and connects parameter callbacks for the Orsay server.
        Needs to be called after connection and reconnection to the server.
        """
        self._gis = self.parent.datamodel.HybridGIS
        self._errorPar = self._gis.ErrorState
        self._positionPar = self._gis.PositionState
        self._reservoirPar = self._gis.ReservoirState
        self._errorPar.Subscribe(self._updateErrorState)
        self._positionPar.Subscribe(self._updatePosition)
        self._reservoirPar.Subscribe(self._updateInjectingGas)
        self.update_VAs()

    def update_VAs(self):
        """
        Update the VA's. Should be called after reconnection to the server
        """
        self._updateErrorState()
        self._updatePosition()
        self._updateInjectingGas()

    def _updateErrorState(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the error state from the Orsay server and saves it in the state VA
        """
        if parameter is None:
            parameter = self._errorPar
        if parameter is not self._errorPar:
            raise ValueError("Incorrect parameter passed to _updateErrorState. Parameter should be "
                             "datamodel.HybridGIS.ErrorState. Parameter passed is %s." % parameter.Name)
        if not attributeName == "Actual":
            return
        if self._errorPar.Actual not in NO_ERROR_VALUES:
            self.state._set_value(HwError(self._errorPar.Actual), force_write=True)
        else:
            self.state._set_value(model.ST_RUNNING, force_write=True)

    def _updatePosition(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the position of the GIS from the Orsay server and saves it in the position VA
        """
        if parameter is None:
            parameter = self._positionPar
        if parameter is not self._positionPar:
            raise ValueError("Incorrect parameter passed to _updatePosition. Parameter should be "
                             "datamodel.HybridGIS.PositionState. Parameter passed is %s." % parameter.Name)
        if attributeName == "Actual":
            pos = self._positionPar.Actual
            logging.debug("Current position is %s." % pos)
            self.position._set_value({"arm": pos == STR_WORK}, force_write=True)
        if self._positionPar.Actual == self._positionPar.Target:
            logging.debug("Target position reached.")
            self._armPositionReached.set()
        else:
            self._armPositionReached.clear()

    def _updateInjectingGas(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the GIS gas flow state from the Orsay server and saves it in the injectingGas VA
        """
        if parameter is None:
            parameter = self._reservoirPar
        if parameter is not self._reservoirPar:
            raise ValueError("Incorrect parameter passed to _updateInjectingGas. Parameter should be "
                             "datamodel.HybridGIS.ReservoirState. Parameter passed is %s." % parameter.Name)
        if not attributeName == "Actual":
            return
        logging.debug("Gas flow is now %s." % self._reservoirPar.Actual)
        new_value = self._reservoirPar.Actual == STR_OPEN
        self.injectingGas._value = new_value  # to not call the setter
        self.injectingGas.notify(new_value)

    def _setInjectingGas(self, goal):
        """
        goal (bool): the goal state of the gas flow: (True: "open", False: "closed")
        return (bool): the new state the gas flow: (True: "open", False: "closed")

        Opens the GIS reservoir if argument goal is True. Closes it otherwise.
        Also closes the reservoir if the position of the GIS is not engaged.
        """
        if not self.position.value["arm"]:
            logging.warning("Gas flow was attempted to be changed while not in working position.")
            goal = False
        if goal:
            logging.debug("Starting gas flow.")
            self._reservoirPar.Target = STR_OPEN
        else:
            logging.debug("Stopping gas flow.")
            self._reservoirPar.Target = STR_CLOSED
        return self._reservoirPar.Target == STR_OPEN

    def _setArm(self, goal):
        """
        goal (bool): the goal state of the GIS position: (True: "engaged", False: "parked")
        return (bool): the state the GIS position is in after finishing moving: (True: STR_WORK, False: STR_PARK)

        Moves the GIS to working position if argument goal is True. Moves it to parking position otherwise.
        """
        if self.injectingGas.value:
            logging.warning("Gas flow was still on when attempting to move the GIS. Turning off gas flow.")
            self.injectingGas.value = False  # turn off the gas flow if it wasn't already
        self._armPositionReached.clear()  # to assure it waits
        if goal:
            logging.debug("Moving GIS to working position.")
            self._positionPar.Target = STR_WORK
        else:
            logging.debug("Moving GIS to parking position.")
            self._positionPar.Target = STR_PARK
        self._armPositionReached.wait()
        self._updatePosition()

    @isasync
    def moveAbs(self, pos):
        """
        Move the axis of this actuator to pos.
        """
        self._checkMoveAbs(pos)
        return self._executor.submit(self._setArm, goal=pos["arm"])

    @isasync
    def moveRel(self, shift):
        """
        Move the axis of this actuator by shift.
        """
        raise NotImplementedError("Relative movements are not implemented for the arm position. Use moveAbs instead.")

    def stop(self, axes=None):
        """
        Stop the GIS. This will turn off temperature regulation (RegulationOn.Target = False),
        close the reservoir valve (ReservoirState.Target = STR_CLOSED)
        and move the GIS away from the sample (PositionState.Target = STR_PARK).
        """
        if axes is None or "arm" in axes:
            self._gis.Stop.Target = COMPONENT_STOP
            self._executor.cancel()

    def terminate(self):
        """
        Called when Odemis is closed
        """
        if self._gis:
            self._errorPar.Unsubscribe(self._updateErrorState)
            self._positionPar.Unsubscribe(self._updatePosition)
            self._reservoirPar.Unsubscribe(self._updateInjectingGas)
            if self._executor:
                self._executor.shutdown()
                self._executor = None
            self._errorPar = None
            self._positionPar = None
            self._reservoirPar = None
            self._gis = None


class GISReservoir(model.HwComponent):
    """
    This represents the GIS gas reservoir from Orsay Physics
    """

    def __init__(self, name, role, parent, **kwargs):
        """
        Defines the following VA's and links them to the callbacks from the Orsay server:
        • temperatureTarget: FloatContinuous, unit="°C", range=(-273.15, 10^3), setter=_setTemperatureTarget
        • temperature: FloatContinuous, readonly, unit="°C", range=(-273.15, 10^3)
        • temperatureRegulation: EnumeratedVA, unit=None, choices={0: "off", 1: "on", 2: "rush"},
                                 setter=_setTemperatureRegulation
        • age: FloatContinuous, readonly, unit="s", range=(0, 10^12)
        • precursorType: StringVA, readonly
        """

        model.HwComponent.__init__(self, name, role, parent=parent, **kwargs)

        self._gis = None
        self._temperaturePar = None

        self.temperatureTarget = model.FloatContinuous(0, unit="°C", range=(-273.15, 10 ** 3),
                                                       setter=self._setTemperatureTarget)
        self.temperature = model.FloatContinuous(0, unit="°C", range=(-273.15, 10 ** 3), readonly=True)
        self.temperatureRegulation = model.VAEnumerated(0, unit=None, choices={0: "off", 1: "on", 2: "rush"},
                                                        setter=self._setTemperatureRegulation)
        self.age = model.FloatContinuous(0, unit="s", readonly=True, range=(0, 10 ** 12))
        self.precursorType = model.StringVA("", readonly=True)

        self.on_connect()

    def on_connect(self):
        """
        Defines direct pointers to server components and connects parameter callbacks for the Orsay server.
        Needs to be called after connection and reconnection to the server.
        """
        self._gis = self.parent.datamodel.HybridGIS
        self._temperaturePar = self._gis.ReservoirTemperature

        self._gis.ErrorState.Subscribe(self._updateErrorState)
        self._gis.RodPosition.Subscribe(self._updateErrorState)
        self._temperaturePar.Subscribe(self._updateTemperatureTarget)
        self._temperaturePar.Subscribe(self._updateTemperature)
        self._gis.RegulationOn.Subscribe(self._updateTemperatureRegulation)
        self._gis.RegulationRushOn.Subscribe(self._updateTemperatureRegulation)
        self._gis.ReservoirLifeTime.Subscribe(self._updateAge)
        self._gis.PrecursorType.Subscribe(self._updatePrecursorType)

        self.update_VAs()

    def update_VAs(self):
        """
        Update the VA's. Should be called after reconnection to the server
        """
        self._updateErrorState()
        self._updateTemperatureTarget()
        self._updateTemperature()
        self._updateTemperatureRegulation()
        self._updateAge()
        self._updatePrecursorType()

    def _updateErrorState(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the error state from the Orsay server and saves it in the state VA
        """
        if parameter not in (self._gis.ErrorState, self._gis.RodPosition, None):
            raise ValueError("Incorrect parameter passed to _updateErrorState. Parameter should be "
                             "datamodel.HybridGIS.ErrorState, datamodel.HybridGIS.RodPosition, or None. "
                             "Parameter passed is %s." % parameter.Name)
        if not attributeName == "Actual":
            return

        msg = ""
        try:
            rod_pos = int(self._gis.RodPosition.Actual)
        except TypeError as e:
            logging.warning("Unable to convert RodPosition to integer: %s" % str(e))
            rod_pos = ROD_NOT_DETECTED

        if rod_pos == ROD_NOT_DETECTED:
            msg += "Reservoir rod not detected. "
        elif rod_pos == ROD_RESERVOIR_NOT_STRUCK:
            msg += "Reservoir not struck. "
        elif rod_pos == ROD_READING_ERROR:
            msg += "Error in reading the rod position. "

        if self._gis.ErrorState.Actual not in NO_ERROR_VALUES:
            msg += self._gis.ErrorState.Actual

        if msg == "":
            self.state._set_value(model.ST_RUNNING, force_write=True)
        else:
            self.state._set_value(HwError(msg), force_write=True)

    def _updateTemperatureTarget(self, parameter=None, attributeName="Target"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the target temperature of the GIS reservoir from the Orsay server and saves it in the temperatureTarget VA
        """
        if parameter is None:
            parameter = self._temperaturePar
        if parameter is not self._temperaturePar:
            raise ValueError("Incorrect parameter passed to _updateTemperatureTarget. Parameter should be "
                             "datamodel.HybridGIS.ReservoirTemperature. Parameter passed is %s." % parameter.Name)
        if not attributeName == "Target":
            return
        new_value = float(self._temperaturePar.Target)
        logging.debug("Target temperature changed to %f." % new_value)
        self.temperatureTarget._value = new_value  # to not call the setter
        self.temperatureTarget.notify(new_value)

    def _updateTemperature(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the actual temperature of the GIS reservoir from the Orsay server and saves it in the temperature VA
        """
        if parameter is None:
            parameter = self._temperaturePar
        if parameter is not self._temperaturePar:
            raise ValueError("Incorrect parameter passed to _updateTemperature. Parameter should be "
                             "datamodel.HybridGIS.ReservoirTemperature. Parameter passed is %s." % parameter.Name)

        if float(self._temperaturePar.Actual) == float(self._temperaturePar.Target):
            logging.debug("Target temperature reached.")

        if not attributeName == "Actual":
            return
        self.temperature._set_value(float(self._temperaturePar.Actual), force_write=True)

    def _updateTemperatureRegulation(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the state of temperature regulation of the GIS reservoir from the Orsay server and saves it in the
        temperatureRegulation VA
        """
        if parameter not in (self._gis.RegulationOn, self._gis.RegulationRushOn, None):
            raise ValueError("Incorrect parameter passed to _updateTemperatureRegulation. Parameter should be "
                             "datamodel.HybridGIS.RegulationOn, datamodel.HybridGIS.RegulationRushOn, or None. "
                             "Parameter passed is %s." % parameter.Name)
        if not attributeName == "Actual":
            return

        try:
            rush = self._gis.RegulationRushOn.Actual.lower() == "true"
        except AttributeError:  # in case RegulationRushOn.Actual is not a string
            rush = False
        try:
            reg = self._gis.RegulationOn.Actual.lower() == "true"
        except AttributeError:  # in case RegulationOn.Actual is not a string
            reg = False

        if rush and reg:
            logging.debug("Accelerated temperature regulation turned on.")
            new_value = 2
        elif reg:
            logging.debug("Temperature regulation turned on.")
            new_value = 1
        else:
            logging.debug("Temperature regulation turned off.")
            new_value = 0
        self.temperatureRegulation._value = new_value  # to not call the setter
        self.temperatureRegulation.notify(new_value)

    def _updateAge(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the amount of hours the GIS reservoir has been open for from the Orsay server and saves it in the age VA
        """
        if parameter is None:
            parameter = self._gis.ReservoirLifeTime
        if parameter is not self._gis.ReservoirLifeTime:
            raise ValueError("Incorrect parameter passed to _updateAge. Parameter should be "
                             "datamodel.HybridGIS.ReservoirLifeTime. Parameter passed is %s." % parameter.Name)
        if not attributeName == "Actual":
            return
        logging.debug("GIS reservoir lifetime updated to %f hours." % float(self._gis.ReservoirLifeTime.Actual))
        self.age._set_value(float(self._gis.ReservoirLifeTime.Actual) * 3600,  # convert hours to seconds
                            force_write=True)

    def _updatePrecursorType(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the type of precursor gas in the GIS reservoir from the Orsay server and saves it in the precursorType VA
        """
        if parameter is None:
            parameter = self._gis.PrecursorType
        if parameter is not self._gis.PrecursorType:
            raise ValueError("Incorrect parameter passed to _updatePrecursorType. Parameter should be "
                             "datamodel.HybridGIS.PrecursorType. Parameter passed is %s." % parameter.Name)
        if not attributeName == "Actual":
            return
        logging.debug("Precursor type changed to %s." % self._gis.PrecursorType.Actual)
        self.precursorType._set_value(self._gis.PrecursorType.Actual, force_write=True)

    def _setTemperatureTarget(self, goal):
        """
        goal (float): temperature in °C to set as a target temperature
        return (float): temperature in °C the target temperature is set to

        Sets the target temperature of the GIS reservoir to goal °C
        """
        logging.debug("Setting target temperature to %f." % goal)
        self._temperaturePar.Target = goal
        return float(self._temperaturePar.Target)

    def _setTemperatureRegulation(self, goal):
        """
        goal (int): mode to set the temperature regulation to. Must be 0, 1 or 2

        Turns temperature regulation off (if goal = 0), on (if goal = 1) or on in accelerated mode (if goal = 2)
        """
        reg = False
        rush = False
        if goal == 2:
            logging.debug("Setting temperature regulation to accelerated mode.")
            rush = True
            reg = True
        elif goal == 1:
            logging.debug("Turning on temperature regulation.")
            reg = True
        else:
            logging.debug("Turning off temperature regulation.")
        self._gis.RegulationOn.Target = reg
        self._gis.RegulationRushOn.Target = rush

    def terminate(self):
        """
        Called when Odemis is closed
        """
        if self._gis:
            self._gis.ErrorState.Unsubscribe(self._updateErrorState)
            self._gis.RodPosition.Unsubscribe(self._updateErrorState)
            self._temperaturePar.Unsubscribe(self._updateTemperatureTarget)
            self._temperaturePar.Unsubscribe(self._updateTemperature)
            self._gis.RegulationOn.Unsubscribe(self._updateTemperatureRegulation)
            self._gis.RegulationRushOn.Unsubscribe(self._updateTemperatureRegulation)
            self._gis.ReservoirLifeTime.Unsubscribe(self._updateAge)
            self._gis.PrecursorType.Unsubscribe(self._updatePrecursorType)
            self._temperaturePar = None
            self._gis = None


class OrsayParameterConnector:
    """
    Attribute that is connected to a VA and a parameter on the Orsay server.
    If VA is readonly, the VA will be kept up to date of the changes of the Orsay parameter, but force writing to the VA
    will not update the Orsay parameter.
    If VA is not readonly, writing to the VA will write this value to the Orsay parameter's Target attribute.
    """

    def __init__(self, va, parameter, attributeName="Actual", conversion=None):
        """
        va is the vigilant attribute this Orsay parameter connector should be connected to. This VA should not have a
        setter yet. The setter will be overwritten.
        parameter is a parameter of the Orsay server. It can also be a list of parameters, if va contains a Tuple of
        equal length.
        attributeName is the name of the attribute of parameter the va should be synchronised with. Defaults to "Actual"
        conversion is a dict mapping values of the VA (dict keys) to values of the parameter (dict values). If None is
        supplied, no special conversion will be performed.
        """
        self._parameters = None
        self._attributeName = None
        self._va = None
        self._va_type_name = None
        self._va_is_tuple = False
        self._va_value_type = None
        self._conversion = conversion
        self.connect(va, parameter, attributeName)

    def connect(self, va, parameter, attributeName="Actual"):
        """
        va is the vigilant attribute this Orsay parameter connector should be connected to. This VA should not have a
        setter yet. The setter will be overwritten.
        parameter is a parameter of the Orsay server. It can also be a list of parameters, if va contains a Tuple of
        equal length.
        attributeName is the name of the attribute of parameter the va should be synchronised with. Defaults to "Actual"

        Subscribes the VA to the parameter
        """
        if self._parameters is not None and None not in {self._attributeName, self._va, self._va_type_name}:
            logging.warning("OrsayParameterConnector is already connected to an Orsay parameter. It is better to call "
                            "disconnect before reconnecting to something else.")

        if type(parameter) in {set, list, tuple}:
            self._parameters = list(parameter)
        else:
            self._parameters = [parameter]
        if len(self._parameters) == 0:
            raise ValueError("No parameters passed")
        self._attributeName = attributeName
        self._va = va
        self._va_type_name = va.__class__.__name__
        if self._va_type_name.startswith("Tuple"):
            self._va_is_tuple = True
            self._va_value_type = type(self._va.value[0])
        else:
            self._va_is_tuple = False
            self._va_value_type = type(self._va.value)
        if not self._va.readonly:
            self._va._setter = WeakMethod(self._update_parameter)
        if self._va_is_tuple and not len(self._parameters) == len(self._va.value):
            raise ValueError("Length of Tuple VA does not match number of parameters passed.")
        if len(self._parameters) > 1 and not self._va_is_tuple:
            raise ValueError("Multiple parameters are passed, but VA is not of a tuple type.")

        for p in self._parameters:
            p.Subscribe(self.update_VA)

        self.update_VA()

    def disconnect(self):
        """
        Unsubscribes the VA from the parameter
        """
        if self._va is not None and self._parameters is not None:
            for p in self._parameters:
                p.Unsubscribe(self.update_VA)
            self._parameters = None
            self._attributeName = None
            self._va._setter = WeakMethod(self._va._VigilantAttribute__default_setter)
            self._va = None
            self._va_type_name = None
            self._conversion = None

    def update_VA(self, parameter=None, attributeName=None):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server that calls this callback
        attributeName (str): the name of the attribute of parameter which was changed

        Copies the value of the parameter to the VA
        """
        if self._parameters is None or None in {self._attributeName, self._va, self._va_type_name}:
            raise AttributeError("OrsayParameterConnector is not connected to an Orsay parameter. "
                                 "Call this object's connect method before calling update.")

        if attributeName is None:
            attributeName = self._attributeName
        if not attributeName == self._attributeName:
            return

        names = [(p.Name + ", ") for p in self._parameters]
        namesstring = ""
        namesstring = namesstring.join(names)[:-2]
        if parameter is not None and parameter not in self._parameters:
            raise ValueError("Incorrect parameter passed. Excpected: %s. Received: %s."
                             % (namesstring, parameter.Name))

        if self._va_is_tuple:
            new_values = []
            for p in self._parameters:
                new_entry = self._parameter_to_VA_value(getattr(p, attributeName))
                new_values.append(new_entry)
            new_value = tuple(new_values)
        else:
            new_value = self._parameter_to_VA_value(getattr(self._parameters[0], attributeName))

        logging.debug("[%s].%s changed to %s." % (namesstring, attributeName, str(new_value)))
        self._va._value = new_value  # to not call the setter
        self._va.notify(new_value)

    def _update_parameter(self, goal):
        """
        setter of the non-read-only VA. Sets goal to the Orsay parameter's Target and returns goal to set it to the VA
        """
        if self._parameters is None or None in {self._attributeName, self._va, self._va_type_name}:
            raise AttributeError("OrsayParameterConnector is not connected to an Orsay parameter. "
                                 "Call this object's connect method before setting a value to its VA.")

        if self._va.readonly:
            raise NotSettableError("Value is read-only")

        if self._va_is_tuple:
            for i in range(len(self._parameters)):
                self._parameters[i].Target = self._VA_to_parameter_value(goal[i])
        else:
            self._parameters[0].Target = self._VA_to_parameter_value(goal)

        return goal

    def _VA_to_parameter_value(self, va_value):
        """
        Converts a value of the VA to its corresponding value for the parameter
        va_value is the value of the VA
        returns the corresponding value of the parameter
        """
        if self._conversion is not None:  # if a conversion dict is supplied
            try:
                return self._conversion[va_value]
            except KeyError:
                logging.debug("Conversion dictionary does not contain key %s. Sticking to value %s" %
                              (str(va_value), str(va_value)))
        return va_value

    def _parameter_to_VA_value(self, par_value):
        """
        Converts a value of the parameter to its corresponding value for the VA
        par_value is the value of the parameter
        returns the corresponding value of the VA
        """
        if self._conversion is not None:  # if a conversion dict is supplied
            for key, value in self._conversion.items():
                if value == par_value:
                    return key

        if self._va_value_type == float:
            return float(par_value)
        elif self._va_value_type == int:
            return int(par_value)
        elif self._va_value_type == bool:
            return par_value in {True, "True", "true", 1, "1", "ON"}
        else:
            raise NotImplementedError("Handeling of VA's of type %s is not implemented for OrsayParameterConnector."
                                      % self._va_type_name)


class TestDevice(model.HwComponent):
    """
    This represents the Device that needs a VA communicating with an Orsay parameter
    """

    def __init__(self, name, role, parent, **kwargs):
        """
        """

        model.HwComponent.__init__(self, name, role, parent=parent, **kwargs)

        self.testBooleanVA = model.BooleanVA(True)
        self.OrsayBooleanConnector = None
        self.testFloatVA = model.FloatVA(0.0, unit="Pa")
        self.OrsayFloatConnector = None
        self.testIntVA = model.IntVA(0)
        self.OrsayIntConnector = None
        self.testTupleVA = model.TupleVA((0.1, 0.2))
        self.OrsayTupleConnector = None

        self.on_connect()

    def on_connect(self):
        """
        Defines direct pointers to server components and connects parameter callbacks for the Orsay server.
        Needs to be called after connection and reconnection to the server.
        """
        self.OrsayBooleanConnector = OrsayParameterConnector(self.testBooleanVA,
                                                             self.parent.datamodel.Scanner.OperatingMode,
                                                             conversion={True: 1, False: 0})
        self.OrsayFloatConnector = OrsayParameterConnector(self.testFloatVA,
                                                           self.parent.datamodel.HybridPlatform.PumpingSystem.Manometer1.Pressure)
        self.OrsayIntConnector = OrsayParameterConnector(self.testIntVA,
                                                         self.parent.datamodel.HVPSFloatingIon.HeaterState)
        self.OrsayTupleConnector = OrsayParameterConnector(self.testTupleVA, [
            self.parent.datamodel.IonColumnMCS.CondensorSteerer1StigmatorX,
            self.parent.datamodel.IonColumnMCS.CondensorSteerer1StigmatorY])

    def update_VAs(self):
        """
        Update the VA's. Should be called after reconnection to the server
        """
        self.OrsayBooleanConnector.update_VA()
        self.OrsayFloatConnector.update_VA()
        self.OrsayIntConnector.update_VA()
        self.OrsayTupleConnector.update_VA()

    def terminate(self):
        """
        Called when Odemis is closed
        """
        self.OrsayBooleanConnector.disconnect()
        self.OrsayFloatConnector.disconnect()
        self.OrsayIntConnector.disconnect()
        self.OrsayTupleConnector.disconnect()


class FIBSource(model.HwComponent):
    """
    Represents the source of the Focused Ion Beam (FIB) from Orsay Physics
    """

    def __init__(self, name, role, parent, **kwargs):
        """
        TODO: Once we better understand what each parameter does, for each VA:
                - Check whether we actually need to communicate with this parameter, or if we can take it out of the driver
                - Check if it can/should be set as readonly and change this in the unittest too
                - Check that the values it sets to the Orsay server make sense and are correct
                - Check in its unittest that the testvalues are safe and make sense
                - Check that its unittest is hardware safe and this is set correctly
                - Check in its unittest that the settletime is set to an appropriate value
        Defines the following VA's and links them to the callbacks from the Orsay server:
        • interlockTriggered: BooleanVA
        • gunOn: BooleanVA
        • gunPumpOn: BooleanVA
        • columnPumpOn: BooleanVA
        • gunPressure: FloatContinuous, readonly, unit="Pa", range=(0, 11e4)
        • columnPressure: FloatContinuous, readonly, unit="Pa", range=(0, 11e4)
        • lifetime: FloatContinuous, readonly, unit="Ah", range=(0, 10)
        • currentRegulation: BooleanVA
        • sourceCurrent: FloatContinuous, readonly, unit="A", range=(0, 1e-5) (only used if currentRegulation is true)
        • suppressorVoltage: FloatContinuous, unit="V", range=(-2e3, 2e3) (only used if currentRegulation is false)
        • heatingCurrent: FloatContinuous, unit="A", range=(0, 5)
        • heaterState: IntContinuous, range=(0, 10)  TODO: presumably this is not an int at all. What is this?
        • acceleratorVoltage: IntContinuous, unit="V", range=(0, 3e4)
        • energyLink: BooleanVA
        • extractorVoltage: FloatContinuous, unit="V", range=(0, 12e3)
        TODO: MOVE THE BELOW THREE VA'S TO THE DRIVER FOR THE FIB OPTICS
        • mvaPosition: TupleContinuous Float, unit="m", range=[(0, 0), (10, 10)] (mva = multiple variable apperture)
        • mvaStepSize: FloatEnumerated, unit="m", choices={2e-7, 5e-7, 1e-6, 5e-6, 1e-5, 2e-5, 1e-4, 5e-4, 2e-3, 25e-4}
        • apertureSize: FloatEnumerated, unit="m", TODO: This is not implemented yet, since it might still be refactored
                        choices={1e-5, 2e-5, 3e-5, 4e-5, 6e-5, 8e-5, 1e-4, 13e-5, 2e-4, 3e-4, 4e-4, 6e-4, 8e-4, 95e-5}
        """

        model.HwComponent.__init__(self, name, role, parent=parent, **kwargs)

        self._hvps = None
        self._ionColumn = None
        self._gunPump = None
        self._columnPump = None
        self._devices_with_errorstates = None
        self._interlockHVPS = None
        self._interlockChamber = None

        self.interlockTriggered = model.BooleanVA(False, setter=self._resetInterlocks)
        self.gunOn = model.BooleanVA(False)
        self.gunOnConnector = None
        self.gunPumpOn = model.BooleanVA(False)
        self.gunPumpOnConnector = None
        self.columnPumpOn = model.BooleanVA(False)
        self.columnPumpOnConnector = None
        self.gunPressure = model.FloatContinuous(0, readonly=True, unit="Pa", range=VACUUM_PRESSURE_RNG)
        self.gunPressureConnector = None
        self.columnPressure = model.FloatContinuous(0, readonly=True, unit="Pa", range=VACUUM_PRESSURE_RNG)
        self.columnPressureConnector = None
        self.lifetime = model.FloatContinuous(0, readonly=True, unit="Ah", range=(0, 10))
        self.lifetimeConnector = None
        self.currentRegulation = model.BooleanVA(False)
        self.currentRegulationConnector = None
        self.sourceCurrent = model.FloatContinuous(0, readonly=True, unit="A", range=(0, 1e-5))
        self.sourceCurrentConnector = None
        self.suppressorVoltage = model.FloatContinuous(0, unit="V", range=(-2e3, 2e3))
        self.suppressorVoltageConnector = None
        self.heatingCurrent = model.FloatContinuous(0, unit="A", range=(0, 5))
        self.heatingCurrentConnector = None
        # self.heaterState = model.IntContinuous(0, range=(0, 10))
        # self.heaterStateConnector = None
        self.acceleratorVoltage = model.IntContinuous(0, unit="V", range=(0, 3e4))
        self.acceleratorVoltageConnector = None
        self.energyLink = model.BooleanVA(False)
        self.energyLinkConnector = None
        self.extractorVoltage = model.FloatContinuous(0, unit="V", range=(0, 12e3))
        self.extractorVoltageConnector = None
        self.mvaPosition = model.TupleContinuous((0.0, 0.0), unit="m", range=[(0, 0), (10, 10)])
        self.mvaPositionConnector = None
        self.mvaStepSize = model.FloatEnumerated(1e-6, unit="m",
                                                 choices={2e-7, 5e-7, 1e-6, 5e-6, 1e-5, 2e-5, 1e-4, 5e-4, 2e-3, 25e-4})
        self.mvaStepSizeConnector = None

        self._connectorList = []

        self.on_connect()

    def on_connect(self):
        """
        Defines direct pointers to server components and connects parameter callbacks for the Orsay server.
        Needs to be called after connection and reconnection to the server.
        """

        self._hvps = self.parent.datamodel.HVPSFloatingIon
        self._ionColumn = self.parent.datamodel.IonColumnMCS
        self._gunPump = self.parent.datamodel.HybridIonPumpGunFIB
        self._columnPump = self.parent.datamodel.HybridIonPumpColumnFIB
        self._interlockHVPS = self.parent.datamodel.HybridInterlockOutHVPS
        self._interlockChamber = self.parent.datamodel.HybridInterlockInChamberVac
        self._devices_with_errorstates = ("HybridGaugeCompressedAir",
                                          "HybridInterlockOutHVPS",
                                          "HybridInterlockInChamberVac",
                                          "HybridIonPumpGunFIB",
                                          "HybridIonPumpColumnFIB",
                                          "HybridValveFIB")

        self._interlockHVPS.ErrorState.Subscribe(self._updateInterlockTriggered)
        self._interlockChamber.ErrorState.Subscribe(self._updateInterlockTriggered)
        for device in self._devices_with_errorstates:
            p = getattr(self.parent.datamodel, device).ErrorState
            p.Subscribe(self._updateErrorState)

        self.gunOnConnector = OrsayParameterConnector(self.gunOn, self._hvps.GunState,
                                                      conversion={True: "ON", False: "OFF"})
        self.gunPumpOnConnector = OrsayParameterConnector(self.gunPumpOn, self._gunPump.IsOn)
        self.columnPumpOnConnector = OrsayParameterConnector(self.columnPumpOn, self._columnPump.IsOn)
        self.gunPressureConnector = OrsayParameterConnector(self.gunPressure, self._gunPump.Pressure)
        self.columnPressureConnector = OrsayParameterConnector(self.columnPressure, self._columnPump.Pressure)
        self.lifetimeConnector = OrsayParameterConnector(self.lifetime, self._hvps.SourceLifeTime)
        self.currentRegulationConnector = OrsayParameterConnector(self.currentRegulation,
                                                                  self._hvps.BeamCurrent_Enabled)
        self.sourceCurrentConnector = OrsayParameterConnector(self.sourceCurrent, self._hvps.BeamCurrent)
        self.suppressorVoltageConnector = OrsayParameterConnector(self.suppressorVoltage, self._hvps.Suppressor)
        self.heatingCurrentConnector = OrsayParameterConnector(self.heatingCurrent, self._hvps.Heater)
        # self.heaterStateConnector = OrsayParameterConnector(self.heaterState, self._hvps.HeaterState)
        self.acceleratorVoltageConnector = OrsayParameterConnector(self.acceleratorVoltage, self._hvps.Energy)
        self.energyLinkConnector = OrsayParameterConnector(self.energyLink, self._hvps.EnergyLink,
                                                           conversion={True: "ON", False: "OFF"})
        self.extractorVoltageConnector = OrsayParameterConnector(self.extractorVoltage, self._hvps.Extractor)
        self.mvaPositionConnector = OrsayParameterConnector(self.mvaPosition,
                                                            [self._ionColumn.MCSProbe_X, self._ionColumn.MCSProbe_Y])
        self.mvaStepSizeConnector = OrsayParameterConnector(self.mvaStepSize, self._ionColumn.MCSProbe_Step)

        self._connectorList = [x for (x, _) in  # save only the names of the returned members
                               inspect.getmembers(self,  # get all members of this FIB_source object
                                                  lambda obj: type(obj) == OrsayParameterConnector  # get only the
                                                  # OrsayParameterConnectors from all members of this FIB_source object
                                                  )
                               ]

    def update_VAs(self):
        """
        Update the VA's. Should be called after reconnection to the server
        """
        self._updateErrorState()
        self._updateInterlockTriggered()
        for obj_name in self._connectorList:
            getattr(self, obj_name).update_VA()

    def _updateErrorState(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the error state from the Orsay server and saves it in the state VA
        """
        errorParameters = (getattr(self.parent.datamodel, device).ErrorState
                           for device in self._devices_with_errorstates)
        if parameter is not None and parameter not in errorParameters:
            raise ValueError("Incorrect parameter passed to _updateErrorState. Parameter should be None or a FIB "
                             "related ErrorState parameter. Parameter passed is %s"
                             % parameter.Name)
        if attributeName != "Actual":
            return

        eState = ""
        for device in self._devices_with_errorstates:
            this_state = getattr(self.parent.datamodel, device).ErrorState.Actual
            if this_state not in NO_ERROR_VALUES:
                if not eState == "":
                    eState += ", "
                eState += "%s error: %s" % (device, this_state)

        if eState == "":
            self.state._set_value(model.ST_RUNNING, force_write=True)
        else:
            self.state._set_value(HwError(eState), force_write=True)

    def _updateInterlockTriggered(self, parameter=None, attributeName="Actual"):
        """
        parameter (Orsay Parameter): the parameter on the Orsay server to use to update the VA
        attributeName (str): the name of the attribute of parameter which was changed

        Reads the state of the FIB related interlocks from the Orsay server and saves it in the interlockTriggered VA
        """
        if parameter is not None and parameter not in (
                self._interlockHVPS.ErrorState, self._interlockChamber.ErrorState):
            raise ValueError("Incorrect parameter passed to _updateInterlockTriggered. Parameter should be None or an "
                             "interlock ErrorState parameter. Parameter passed is %s"
                             % parameter.Name)
        if attributeName != "Actual":
            return

        new_value = False
        if (self._interlockHVPS.ErrorState.Actual not in NO_ERROR_VALUES and
            INTERLOCK_DETECTED_STR in self._interlockHVPS.ErrorState.Actual) or \
                (self._interlockChamber.ErrorState.Actual not in NO_ERROR_VALUES and
                 INTERLOCK_DETECTED_STR in self._interlockChamber.ErrorState.Actual):
            new_value = True

        self.interlockTriggered._value = new_value  # to not call the setter
        self.interlockTriggered.notify(new_value)

    def _resetInterlocks(self, value):
        """
        setter for interlockTriggered VA
        value is the value set to the VA
        returns the same value

        Call with value=True to attempt to reset the FIB related interlocks.
        If the reset is successful, _updateInterlockTriggered will be called and the VA will be updated by that.
        """
        if value:
            self._interlockHVPS.Reset.Target = ""
            self._interlockChamber.Reset.Target = ""
        return False

    def terminate(self):
        """
        Called when Odemis is closed
        """
        if self._hvps is not None:
            for obj_name in self._connectorList:
                getattr(self, obj_name).disconnect()
            self._connectorList = []
            self._hvps = None
            self._ionColumn = None
            self._gunPump = None
            self._columnPump = None
            self._devices_with_errorstates = None
            self._interlockHVPS = None
            self._interlockChamber = None
