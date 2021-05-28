#!/usr/bin/env python3
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
import logging
import os
import unittest
import typing

from time import sleep
from odemis.driver import orsay
from odemis.model import HwError
from odemis import model

TEST_NOHW = os.environ.get("TEST_NOHW", 0)  # Default to Hw testing
if not TEST_NOHW == "sim":
    TEST_NOHW = TEST_NOHW == "1"  # make sure values other than "sim", 0 and 1 are converted to 0

TEST_NOHW = "sim"  # TODO: DELETE THIS LINE

CONFIG_PSUS = {"name": "pneumatic-suspension", "role": "pneumatic-suspension"}
CONFIG_PRESSURE = {"name": "pressure", "role": "chamber"}
CONFIG_PSYS = {"name": "pumping-system", "role": "pumping-system"}
CONFIG_UPS = {"name": "ups", "role": "ups"}
CONFIG_GIS = {"name": "gis", "role": "gis"}
CONFIG_GISRES = {"name": "gis-reservoir", "role": "gis-reservoir"}
CONFIG_FIBSOURCE = {"name": "fib-source", "role": "fib-source"}

CONFIG_ORSAY = {"name": "Orsay", "role": "orsay", "host": "192.168.56.101",
                "children": {"pneumatic-suspension": CONFIG_PSUS,
                             "pressure": CONFIG_PRESSURE,
                             "pumping-system": CONFIG_PSYS,
                             "ups": CONFIG_UPS,
                             "gis": CONFIG_GIS,
                             "gis-reservoir": CONFIG_GISRES,
                             "fib-source": CONFIG_FIBSOURCE}
                }

CONFIG_TEST = {"name": "test", "role": "test"}

CONFIG_ORSAY_TEST = {"name": "Orsay", "role": "orsay", "host": "192.168.56.101",
                     "children": {"pneumatic-suspension": CONFIG_PSUS,
                                  "pressure": CONFIG_PRESSURE,
                                  "pumping-system": CONFIG_PSYS,
                                  "ups": CONFIG_UPS,
                                  "gis": CONFIG_GIS,
                                  "gis-reservoir": CONFIG_GISRES,
                                  "fib-source": CONFIG_FIBSOURCE,
                                  "test": CONFIG_TEST}
                     }


class TestOrsayStatic(unittest.TestCase):
    """
    Tests which don't need an Orsay component ready
    """

    def test_creation(self):
        """
        Test to create an Orsay component
        """
        if TEST_NOHW == 1:
            self.skipTest("TEST_NOHW is set. No server to contact.")
        try:
            oserver = orsay.OrsayComponent(**CONFIG_ORSAY)
        except Exception as e:
            self.fail(e)
        self.assertEqual(len(oserver.children.value), 6)

        oserver.terminate()

    def test_wrong_ip(self):
        """
        Tests that an HwError is raised when an empty ip address is entered
        """
        with self.assertRaises(HwError):
            orsay.OrsayComponent(name="Orsay", role="orsay", host="", children="")


class TestOrsay(unittest.TestCase):
    """
    Tests to run on the main Orsay component
    """

    oserver = None

    @classmethod
    def setUpClass(cls):
        """
        Setup the Orsay client
        """
        if TEST_NOHW == 1:
            raise unittest.SkipTest("TEST_NOHW is set. No server to contact.")
        cls.oserver = orsay.OrsayComponent(**CONFIG_ORSAY)

        cls.datamodel = cls.oserver.datamodel

        for child in cls.oserver.children.value:
            if child.name == CONFIG_PSUS["name"]:
                cls.psus = child
            elif child.name == CONFIG_PRESSURE["name"]:
                cls.pressure = child
            elif child.name == CONFIG_PSYS["name"]:
                cls.psys = child
            elif child.name == CONFIG_UPS["name"]:
                cls.ups = child
            elif child.name == CONFIG_GIS["name"]:
                cls.gis = child
            elif child.name == CONFIG_GISRES["name"]:
                cls.gis_res = child

    @classmethod
    def tearDownClass(cls):
        """
        Terminate the Orsay client
        """
        cls.oserver.terminate()

    def test_updateProcessInfo(self):
        """
        Check that the processInfo VA is updated properly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.oserver._updateProcessInfo(self.datamodel.HybridPlatform.Cancel)
        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, cannot force data on Actual parameters of Orsay server "
                          "outside of simulation.")
        test_string = "Some process information"
        self.datamodel.HybridPlatform.ProcessInfo.Actual = test_string
        sleep(1)
        self.assertEqual(self.oserver.processInfo.value, test_string)
        self.datamodel.HybridPlatform.ProcessInfo.Actual = ""

    def test_reconnection(self):
        """
        Checks that after reconnection things still work
        """
        self.oserver._device.HttpConnection.close()  # close the connection
        self.oserver._device.MessageConnection.Connection.close()
        self.oserver._device.DataConnection.Connection.close()
        self.oserver._device.MessageConnection.dataConnection.Connection.close()
        sleep(1)
        while not self.oserver.state.value == model.ST_RUNNING:
            sleep(2)  # wait for the reconnection

        # perform some test to check writing and reading still works
        self.psus._valve.Target = orsay.VALVE_OPEN
        sleep(1)
        self.assertTrue(self.psus.power.value)

        self.psus._valve.Target = orsay.VALVE_CLOSED
        sleep(1)
        self.assertFalse(self.psus.power.value)

        self.psus.power.value = True
        sleep(1)
        self.assertEqual(int(self.psus._valve.Target), orsay.VALVE_OPEN)

        self.psus.power.value = False
        sleep(1)
        self.assertEqual(int(self.psus._valve.Target), orsay.VALVE_CLOSED)


class TestPneumaticSuspension(unittest.TestCase):
    """
    Tests for the pneumatic suspension
    """

    oserver = None

    @classmethod
    def setUpClass(cls):
        """
        Setup the Orsay client
        """
        if TEST_NOHW == 1:
            raise unittest.SkipTest("TEST_NOHW is set. No server to contact.")
        cls.oserver = orsay.OrsayComponent(**CONFIG_ORSAY)

        cls.datamodel = cls.oserver.datamodel

        for child in cls.oserver.children.value:
            if child.name == CONFIG_PSUS["name"]:
                cls.psus = child

    @classmethod
    def tearDownClass(cls):
        """
        Terminate the Orsay client
        """
        cls.oserver.terminate()

    def test_valve(self):
        """
        Test for controlling the power valve
        """
        self.psus._valve.Target = orsay.VALVE_OPEN
        sleep(1)
        self.assertTrue(self.psus.power.value)

        self.psus._valve.Target = orsay.VALVE_CLOSED
        sleep(1)
        self.assertFalse(self.psus.power.value)

        self.psus.power.value = True
        sleep(1)
        self.assertEqual(int(self.psus._valve.Target), orsay.VALVE_OPEN)

        self.psus.power.value = False
        sleep(1)
        self.assertEqual(int(self.psus._valve.Target), orsay.VALVE_CLOSED)

    def test_errorstate(self):
        """
        Check that the state VA is updated properly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.psus._updateErrorState(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, cannot force data on Actual parameters of Orsay server "
                          "outside of simulation.")
        test_string = "This thing broke"

        self.datamodel.HybridPlatform.Manometer2.ErrorState.Actual = test_string
        sleep(1)
        self.assertIsInstance(self.psus.state.value, HwError)
        self.assertIn("Manometer2", str(self.psus.state.value))
        self.assertIn(test_string, str(self.psus.state.value))
        self.datamodel.HybridPlatform.Manometer2.ErrorState.Actual = ""

        self.datamodel.HybridPlatform.ValvePneumaticSuspension.ErrorState.Actual = test_string
        sleep(1)
        self.assertIsInstance(self.psus.state.value, HwError)
        self.assertIn("ValvePneumaticSuspension", str(self.psus.state.value))
        self.assertIn(test_string, str(self.psus.state.value))
        self.datamodel.HybridPlatform.ValvePneumaticSuspension.ErrorState.Actual = ""

        self.psus._valve.Target = 3
        sleep(1)
        self.assertIsInstance(self.psus.state.value, HwError)
        self.assertIn("ValvePneumaticSuspension is in error", str(self.psus.state.value))
        self.psus._valve.Target = -1
        sleep(1)
        self.assertIsInstance(self.psus.state.value, HwError)
        self.assertIn("ValvePneumaticSuspension could not be contacted", str(self.psus.state.value))
        self.psus._valve.Target = orsay.VALVE_OPEN
        sleep(5)
        self.assertEqual(self.psus.state.value, model.ST_RUNNING)

    def test_updatePower(self):
        """
        Check that the power VA is updated correctly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.psus._updatePower(self.datamodel.HybridPlatform.Cancel)

        self.psus._valve.Target = orsay.VALVE_OPEN
        sleep(1)
        self.assertTrue(self.psus.power.value)

        self.psus._valve.Target = orsay.VALVE_CLOSED
        sleep(1)
        self.assertFalse(self.psus.power.value)

    def test_updatePressure(self):
        """
        Check that the pressure VA is updated correctly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.psus._updatePressure(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, cannot force data on Actual parameters of Orsay server "
                          "outside of simulation.")
        test_value = 1.0
        self.psus._gauge.Actual = test_value
        sleep(1)
        self.assertEqual(self.psus.pressure.value, test_value)
        self.psus._gauge.Actual = 0.0


class TestVacuumChamber(unittest.TestCase):
    """
    Tests for the vacuum chamber
    """

    oserver = None

    @classmethod
    def setUpClass(cls):
        """
        Setup the Orsay client
        """
        if TEST_NOHW == 1:
            raise unittest.SkipTest("TEST_NOHW is set. No server to contact.")
        cls.oserver = orsay.OrsayComponent(**CONFIG_ORSAY)

        cls.datamodel = cls.oserver.datamodel

        for child in cls.oserver.children.value:
            if child.name == CONFIG_PRESSURE["name"]:
                cls.pressure = child

    @classmethod
    def tearDownClass(cls):
        """
        Terminate the Orsay client
        """
        cls.oserver.terminate()

    def test_valve(self):
        """
        Test for controlling the gate valve of the chamber
        """
        self.pressure._gate.IsOpen.Target = orsay.VALVE_OPEN
        sleep(1)
        self.assertTrue(self.pressure.gateOpen.value)

        self.pressure._gate.IsOpen.Target = orsay.VALVE_CLOSED
        sleep(1)
        self.assertFalse(self.pressure.gateOpen.value)

        self.pressure.gateOpen.value = True
        sleep(1)
        self.assertEqual(int(self.pressure._gate.IsOpen.Target), orsay.VALVE_OPEN)

        self.pressure.gateOpen.value = False
        sleep(1)
        self.assertEqual(int(self.pressure._gate.IsOpen.Target), orsay.VALVE_CLOSED)

    def test_vacuum_sim(self):
        """
        Test for controlling the vacuum that can be run in simulation and on the real system
        """
        self.pressure.moveAbs({"vacuum": 1})
        sleep(1)
        self.assertEqual(int(self.pressure._chamber.VacuumStatus.Target), 1)
        self.pressure.stop()

        self.pressure.moveAbs({"vacuum": 2})
        sleep(1)
        self.assertEqual(int(self.pressure._chamber.VacuumStatus.Target), 2)
        self.pressure.stop()

        self.pressure.moveAbs({"vacuum": 0})
        sleep(1)
        self.assertEqual(int(self.pressure._chamber.VacuumStatus.Target), 0)
        self.pressure.stop()

    def test_vacuum_real(self):
        """
        Test for controlling the real vacuum
        TODO: Tune the goal pressure and allowed difference (delta) of all vacuum statuses in this test!!!
              Tune these such that they are realistic and appropriate for primary vacuum, high vacuum or vented chamber.
        """
        if TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is set, cannot change vacuum pressure in simulation")

        pressure_primary = 50000  # TODO: Tune this to primary vacuum!
        delta_primary = 5000  # TODO: Tune this to primary vacuum!
        pressure_high = 0.1  # TODO: Tune this to high vacuum!
        delta_high = 0.01  # TODO: Tune this to high vacuum!
        pressure_vented = 100000  # TODO: Tune this to vented chamber!
        delta_vented = 10000  # TODO: Tune this to vented chamber!

        f = self.pressure.moveAbs({"vacuum": 1})  # go to primary vacuum
        f.result()
        self.assertEqual(self.pressure.position.value["vacuum"], 1)  # check that primary vacuum is reached
        self.assertAlmostEqual(self.pressure.pressure.value, pressure_primary, delta=delta_primary)

        f = self.pressure.moveAbs({"vacuum": 2})  # go to high vacuum
        f.result()
        self.assertEqual(self.pressure.position.value["vacuum"], 2)  # check that high vacuum is reached
        self.assertAlmostEqual(self.pressure.pressure.value, pressure_high, delta=delta_high)

        f = self.pressure.moveAbs({"vacuum": 0})  # vent chamber
        f.result()
        self.assertEqual(self.pressure.position.value["vacuum"], 0)  # check that the chamber is vented
        self.assertAlmostEqual(self.pressure.pressure.value, pressure_vented, delta=delta_vented)

        self.pressure.moveAbs({"vacuum": 1})  # go to primary vacuum
        f = self.pressure.moveAbs({"vacuum": 0})  # immediately vent the chamber
        f.result()
        self.assertEqual(self.pressure.position.value["vacuum"], 0)  # check that the chamber is vented
        self.assertAlmostEqual(self.pressure.pressure.value, pressure_vented, delta=delta_vented)

        self.pressure.moveAbs({"vacuum": 1})  # go to primary vacuum
        sleep(5)
        self.pressure.stop()
        self.assertEqual(self.pressure.position.value["vacuum"], 0)  # check that the chamber is vented
        self.assertAlmostEqual(self.pressure.pressure.value, pressure_vented, delta=delta_vented)

    def test_errorstate(self):
        """
        Check that the state VA is updated properly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.pressure._updateErrorState(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, cannot force data on Actual parameters of Orsay server "
                          "outside of simulation.")
        test_string = "This thing broke"

        self.pressure._gate.ErrorState.Actual = test_string
        sleep(1)
        self.assertIsInstance(self.pressure.state.value, HwError)
        self.assertIn("ValveP5", str(self.pressure.state.value))
        self.assertIn(test_string, str(self.pressure.state.value))
        self.pressure._gate.ErrorState.Actual = ""

        self.pressure._gate.IsOpen.Target = 3
        sleep(1)
        self.assertIsInstance(self.pressure.state.value, HwError)
        self.assertIn("ValveP5 is in error", str(self.pressure.state.value))
        self.pressure._gate.IsOpen.Target = -1
        sleep(1)
        self.assertIsInstance(self.pressure.state.value, HwError)
        self.assertIn("ValveP5 could not be contacted", str(self.pressure.state.value))
        self.pressure._gate.IsOpen.Target = orsay.VALVE_OPEN
        sleep(5)
        self.assertEqual(self.pressure.state.value, model.ST_RUNNING)

    def test_updatePressure(self):
        """
        Check that the pressure VA is updated correctly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.pressure._updatePressure(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, cannot force data on Actual parameters of Orsay server "
                          "outside of simulation.")
        test_value = 1.0
        self.pressure._chamber.Pressure.Actual = test_value
        sleep(1)
        self.assertEqual(self.pressure.pressure.value, test_value)
        self.pressure._chamber.Pressure.Actual = 0.0

    def test_updatePosition(self):
        """
        Check that the position VA is updated correctly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.pressure._updatePosition(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, cannot force data on Actual parameters of Orsay server "
                          "outside of simulation.")
        test_value = 1
        self.pressure._chamber.VacuumStatus.Actual = test_value
        sleep(1)
        self.assertEqual(int(self.pressure.position.value['vacuum']), test_value)
        self.pressure._chamber.VacuumStatus.Actual = 0


class TestPumpingSystem(unittest.TestCase):
    """
    Tests for the pumping system
    """

    oserver = None

    @classmethod
    def setUpClass(cls):
        """
        Setup the Orsay client
        """
        if TEST_NOHW == 1:
            raise unittest.SkipTest("TEST_NOHW is set. No server to contact.")
        cls.oserver = orsay.OrsayComponent(**CONFIG_ORSAY)

        cls.datamodel = cls.oserver.datamodel

        for child in cls.oserver.children.value:
            if child.name == CONFIG_PSYS["name"]:
                cls.psys = child

    @classmethod
    def tearDownClass(cls):
        """
        Terminate the Orsay client
        """
        cls.oserver.terminate()

    def test_errorstate(self):
        """
        Check that the state VA is updated properly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.psys._updateErrorState(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, cannot force data on Actual parameters of Orsay server "
                          "outside of simulation.")
        test_string = "This thing broke"

        self.psys._system.Manometer1.ErrorState.Actual = test_string
        sleep(1)
        self.assertIsInstance(self.psys.state.value, HwError)
        self.assertIn("Manometer1", str(self.psys.state.value))
        self.assertIn(test_string, str(self.psys.state.value))
        self.psys._system.Manometer1.ErrorState.Actual = ""

        self.psys._system.TurboPump1.ErrorState.Actual = test_string
        sleep(1)
        self.assertIsInstance(self.psys.state.value, HwError)
        self.assertIn("TurboPump1", str(self.psys.state.value))
        self.assertIn(test_string, str(self.psys.state.value))

        self.psys._system.TurboPump1.ErrorState.Actual = ""
        sleep(1)
        self.assertEqual(self.psys.state.value, model.ST_RUNNING)

    def test_updateSpeed(self):
        """
        Check that the speed VA is updated correctly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.psys._updateSpeed(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, data isn't copied from Target to Actual outside of simulation.")

        test_value = 1.0
        self.psys._system.TurboPump1.Speed.Target = test_value
        sleep(1)
        self.assertEqual(self.psys.speed.value, test_value)
        self.psys._system.TurboPump1.Speed.Target = 0

    def test_updateTemperature(self):
        """
        Check that the temperature VA is updated correctly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.psys._updateTemperature(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, data isn't copied from Target to Actual outside of simulation.")

        test_value = 1.0
        self.psys._system.TurboPump1.Temperature.Target = test_value
        sleep(1)
        self.assertEqual(self.psys.temperature.value, test_value)
        self.psys._system.TurboPump1.Temperature.Target = 0

    def test_updatePower(self):
        """
        Check that the power VA is updated correctly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.psys._updatePower(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, data isn't copied from Target to Actual outside of simulation.")

        test_value = 1.0
        self.psys._system.TurboPump1.Power.Target = test_value
        sleep(1)
        self.assertEqual(self.psys.power.value, test_value)
        self.psys._system.TurboPump1.Power.Target = 0

    def test_updateSpeedReached(self):
        """
        Check that the speedReached VA is updated correctly and an exception is raised when the wrong parameter is
        passed
        """
        with self.assertRaises(ValueError):
            self.psys._updateSpeedReached(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, data isn't copied from Target to Actual outside of simulation.")

        test_value = True
        self.psys._system.TurboPump1.SpeedReached.Target = test_value
        sleep(1)
        self.assertEqual(self.psys.speedReached.value, test_value)
        self.psys._system.TurboPump1.SpeedReached.Target = False

    def test_updateTurboPumpOn(self):
        """
        Check that the turboPumpOn VA is updated correctly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.psys._updateTurboPumpOn(self.datamodel.HybridPlatform.Cancel)

        self.psys._system.TurboPump1.IsOn.Target = True
        sleep(1)
        self.assertTrue(self.psys.turboPumpOn.value)
        self.psys._system.TurboPump1.IsOn.Target = False
        sleep(1)
        self.assertFalse(self.psys.turboPumpOn.value)

    def test_updatePrimaryPumpOn(self):
        """
        Check that the primaryPumpOn VA is updated correctly and an exception is raised when the wrong parameter is
        passed
        """
        with self.assertRaises(ValueError):
            self.psys._updatePrimaryPumpOn(self.datamodel.HybridPlatform.Cancel)

        self.datamodel.HybridPlatform.PrimaryPumpState.Target = True
        sleep(1)
        self.assertTrue(self.psys.primaryPumpOn.value)
        if TEST_NOHW == "sim":  # for some reason simulation does not properly deal with setting Target to False
            self.datamodel.HybridPlatform.PrimaryPumpState.Actual = False
        else:
            self.datamodel.HybridPlatform.PrimaryPumpState.Target = False
        sleep(1)
        self.assertFalse(self.psys.primaryPumpOn.value)

    def test_updateNitrogenPressure(self):
        """
        Check that the nitrogenPressure VA is updated correctly and an exception is raised when the wrong parameter is
        passed
        """
        with self.assertRaises(ValueError):
            self.psys._updateNitrogenPressure(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, data isn't copied from Target to Actual outside of simulation.")
        test_value = 1.0
        self.psys._system.Manometer1.Pressure.Target = test_value
        sleep(1)
        self.assertEqual(self.psys.nitrogenPressure.value, test_value)
        self.psys._system.Manometer1.Pressure.Target = 0


class TestUPS(unittest.TestCase):
    """
    Tests for the uninterupted power supply
    """

    oserver = None

    @classmethod
    def setUpClass(cls):
        """
        Setup the Orsay client
        """
        if TEST_NOHW == 1:
            raise unittest.SkipTest("TEST_NOHW is set. No server to contact.")
        cls.oserver = orsay.OrsayComponent(**CONFIG_ORSAY)

        cls.datamodel = cls.oserver.datamodel

        for child in cls.oserver.children.value:
            if child.name == CONFIG_UPS["name"]:
                cls.ups = child

    @classmethod
    def tearDownClass(cls):
        """
        Terminate the Orsay client
        """
        cls.oserver.terminate()

    def test_updateLevel(self):
        """
        Check that the level VA raises an exception when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.ups._updateLevel(self.datamodel.HybridPlatform.Cancel)


class TestGIS(unittest.TestCase):
    """
    Tests for the gas injection system (GIS)
    """

    oserver = None

    @classmethod
    def setUpClass(cls):
        """
        Setup the Orsay client
        """
        if TEST_NOHW == 1:
            raise unittest.SkipTest("TEST_NOHW is set. No server to contact.")
        cls.oserver = orsay.OrsayComponent(**CONFIG_ORSAY)

        cls.datamodel = cls.oserver.datamodel

        for child in cls.oserver.children.value:
            if child.name == CONFIG_GIS["name"]:
                cls.gis = child

    @classmethod
    def tearDownClass(cls):
        """
        Terminate the Orsay client
        """
        cls.oserver.terminate()

    def test_errorstate(self):
        """
        Check that the state VA is updated properly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.gis._updateErrorState(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, cannot force data on Actual parameters of Orsay server "
                          "outside of simulation.")
        test_string = "This thing broke"

        self.gis._gis.ErrorState.Actual = test_string
        self.assertIsInstance(self.gis.state.value, HwError)
        self.assertIn(test_string, str(self.gis.state.value))

        self.gis._gis.ErrorState.Actual = ""
        sleep(1)
        self.assertEqual(self.gis.state.value, model.ST_RUNNING)

    def test_updatePosition(self):
        """
        Check that the position VA is updated correctly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.gis._updatePosition(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, data isn't copied from Target to Actual outside of simulation.")

        self.gis._gis.PositionState.Target = orsay.STR_WORK
        sleep(1)
        self.assertTrue(self.gis.position.value["arm"])

        self.gis._gis.PositionState.Target = "MOVING"
        sleep(1)
        self.assertFalse(self.gis.position.value["arm"])

        self.gis._gis.PositionState.Target = orsay.STR_PARK
        sleep(1)
        self.assertFalse(self.gis.position.value["arm"])

    def test_updateInjectingGas(self):
        """
        Check that the injectingGas VA is updated correctly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.gis._updateInjectingGas(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, data isn't copied from Target to Actual outside of simulation.")

        self.gis._gis.ReservoirState.Target = orsay.STR_OPEN
        sleep(1)
        self.assertTrue(self.gis.injectingGas.value)

        self.gis._gis.ReservoirState.Target = orsay.STR_CLOSED
        sleep(1)
        self.assertFalse(self.gis.injectingGas.value)

    def test_moveAbs(self):
        """
        Test movement of the gis to working position and parking position
        """
        f = self.gis.moveAbs({"arm": True})
        f.result()
        self.assertTrue(self.gis.position.value["arm"])

        f = self.gis.moveAbs({"arm": False})
        sleep(1)  # TODO: Tune so the arm has started moving, but is not done yet
        self.assertFalse(self.gis.position.value["arm"])
        f.result()
        self.assertFalse(self.gis.position.value["arm"])

    def test_gasFlow(self):
        """
        Tests the gas flow control and checks that gas flow cannot be started if the gis is not in working position
        """
        f = self.gis.moveAbs({"arm": True})
        f.result()

        self.gis._gis.ReservoirState.Target = orsay.STR_OPEN
        sleep(1)
        self.assertTrue(self.gis.injectingGas.value)
        self.gis._gis.ReservoirState.Target = orsay.STR_CLOSED
        sleep(1)
        self.assertFalse(self.gis.injectingGas.value)

        self.gis.injectingGas.value = True
        sleep(1)
        self.assertEqual(self.gis._gis.ReservoirState.Target, orsay.STR_OPEN)

        self.gis.injectingGas.value = False
        sleep(1)
        self.assertEqual(self.gis._gis.ReservoirState.Target, orsay.STR_CLOSED)

        f = self.gis.moveAbs({"arm": False})
        f.result()

        with self.assertLogs(logger=None, level=logging.WARN):
            self.gis.injectingGas.value = True
        self.assertFalse(self.gis.injectingGas.value)

    def test_stop(self):
        """
        Tests that calling stop has the expected behaviour
        """
        if not TEST_NOHW == 0:
            self.skipTest("No hardware present to test stop function.")

        f = self.gis.moveAbs({"arm": True})
        f.result()
        self.gis.injectingGas.value = True
        sleep(1)
        self.gis.stop()
        sleep(1)
        self.assertFalse(self.gis.injectingGas.value)
        self.assertFalse(self.gis.position.value["arm"])


class TestGISReservoir(unittest.TestCase):
    """
    Tests for the gas injection system (GIS) reservoir
    """

    oserver = None

    @classmethod
    def setUpClass(cls):
        """
        Setup the Orsay client
        """
        if TEST_NOHW == 1:
            raise unittest.SkipTest("TEST_NOHW is set. No server to contact.")

        cls.oserver = orsay.OrsayComponent(**CONFIG_ORSAY)

        cls.datamodel = cls.oserver.datamodel

        for child in cls.oserver.children.value:
            if child.name == CONFIG_GISRES["name"]:
                cls.gis_res = child
            elif child.name == CONFIG_GIS["name"]:
                cls.gis = child

    @classmethod
    def tearDownClass(cls):
        """
        Terminate the Orsay client
        """
        cls.oserver.terminate()

    def test_errorstate(self):
        """
        Check that the state VA is updated properly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.gis_res._updateErrorState(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, cannot force data on Actual parameters of Orsay server "
                          "outside of simulation.")
        test_string = "This thing broke"

        self.gis_res._gis.ErrorState.Actual = test_string
        self.assertIsInstance(self.gis_res.state.value, HwError)
        self.assertIn(test_string, str(self.gis_res.state.value))

        self.gis_res._gis.ErrorState.Actual = ""
        self.gis_res._gis.RodPosition.Actual = 0
        sleep(1)
        self.assertIsInstance(self.gis_res.state.value, HwError)
        self.assertIn("Reservoir rod not detected", str(self.gis_res.state.value))

        self.gis_res._gis.RodPosition.Actual = 1
        sleep(1)
        self.assertIsInstance(self.gis_res.state.value, HwError)
        self.assertIn("Reservoir not struck", str(self.gis_res.state.value))

        self.gis_res._gis.RodPosition.Actual = 3
        sleep(1)
        self.assertIsInstance(self.gis_res.state.value, HwError)
        self.assertIn("Error in reading the rod position", str(self.gis_res.state.value))

        self.gis_res._gis.RodPosition.Actual = 2
        sleep(1)
        self.assertEqual(self.gis_res.state.value, model.ST_RUNNING)

    def test_updateTemperatureTarget(self):
        """
        Check that the temperatureTarget VA is updated correctly and an exception is raised when the wrong parameter is
        passed
        """
        with self.assertRaises(ValueError):
            self.gis_res._updateTemperatureTarget(self.datamodel.HybridPlatform.Cancel)

        test_value = 20
        self.gis_res._temperaturePar.Target = test_value
        sleep(1)
        self.assertEqual(self.gis_res.temperatureTarget.value, test_value)

        self.gis_res._temperaturePar.Target = 0
        sleep(1)
        self.assertEqual(self.gis_res.temperatureTarget.value, 0)

    def test_updateTemperature(self):
        """
        Check that the temperature VA is updated correctly and an exception is raised when the wrong parameter is
        passed
        """
        with self.assertRaises(ValueError):
            self.gis_res._updateTemperature(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, cannot force data on Actual parameters of Orsay server "
                          "outside of simulation.")

        test_value = 20
        self.gis_res._temperaturePar.Target = test_value
        sleep(1)
        self.assertEqual(self.gis_res.temperature.value, test_value)

        self.gis_res._temperaturePar.Target = 0
        sleep(1)
        self.assertEqual(self.gis_res.temperature.value, 0)

    def test_updateTemperatureRegulation(self):
        """
        Check that the temperatureRegulation VA is updated correctly and an exception is raised when the wrong parameter
        is passed
        """
        with self.assertRaises(ValueError):
            self.gis_res._updateTemperatureRegulation(self.datamodel.HybridPlatform.Cancel)

        self.gis_res._gis.RegulationOn.Target = True
        self.gis_res._gis.RegulationRushOn.Target = True
        sleep(1)
        self.assertEqual(self.gis_res.temperatureRegulation.value, 2)

        self.gis_res._gis.RegulationOn.Target = True
        self.gis_res._gis.RegulationRushOn.Target = False
        sleep(1)
        self.assertEqual(self.gis_res.temperatureRegulation.value, 1)

        self.gis_res._gis.RegulationOn.Target = False
        self.gis_res._gis.RegulationRushOn.Target = True
        sleep(1)
        self.assertEqual(self.gis_res.temperatureRegulation.value, 0)

        self.gis_res._gis.RegulationOn.Target = False
        self.gis_res._gis.RegulationRushOn.Target = False
        sleep(1)
        self.assertEqual(self.gis_res.temperatureRegulation.value, 0)

    def test_updateAge(self):
        """
        Check that the age VA is updated correctly and an exception is raised when the wrong parameter is passed
        """
        with self.assertRaises(ValueError):
            self.gis_res._updateAge(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, cannot force data on Actual parameters of Orsay server "
                          "outside of simulation.")

        test_value = 20
        self.gis_res._gis.ReservoirLifeTime.Actual = test_value
        sleep(1)
        self.assertEqual(self.gis_res.age.value, test_value * 3600)

        self.gis_res._gis.ReservoirLifeTime.Actual = 0
        sleep(1)
        self.assertEqual(self.gis_res.age.value, 0)

    def test_updatePrecursorType(self):
        """
        Check that the precursorType VA is updated correctly and an exception is raised when the wrong parameter is
        passed
        """
        with self.assertRaises(ValueError):
            self.gis_res._updatePrecursorType(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("TEST_NOHW is not set to sim, cannot force data on Actual parameters of Orsay server "
                          "outside of simulation.")

        test_value = "test precursor"
        self.gis_res._gis.PrecursorType.Actual = test_value
        sleep(1)
        self.assertEqual(self.gis_res.precursorType.value, test_value)

        self.gis_res._gis.PrecursorType.Actual = "Simulation"
        sleep(1)
        self.assertEqual(self.gis_res.precursorType.value, "Simulation")

    def test_setTemperatureTarget(self):
        """
        Test the setter of the temperatureTarget VA
        """
        test_value = 20
        self.gis_res.temperatureTarget.value = test_value
        sleep(1)
        self.assertEqual(int(self.gis_res._temperaturePar.Target), test_value)

        self.gis_res.temperatureTarget.value = 0
        sleep(1)
        self.assertEqual(int(self.gis_res._temperaturePar.Target), 0)

    def test_setTemperatureRegulation(self):
        """
        Test the setter of the temperatureRegulation VA
        """
        self.gis_res.temperatureRegulation.value = 0
        sleep(1)
        self.assertEqual(self.gis_res.temperatureRegulation.value, 0)
        self.assertFalse(self.gis_res._gis.RegulationOn.Target.lower() == "true")
        self.assertFalse(self.gis_res._gis.RegulationRushOn.Target.lower() == "true")

        self.gis_res.temperatureRegulation.value = 1
        sleep(1)
        self.assertEqual(self.gis_res.temperatureRegulation.value, 1)
        self.assertTrue(self.gis_res._gis.RegulationOn.Target.lower() == "true")
        self.assertFalse(self.gis_res._gis.RegulationRushOn.Target.lower() == "true")

        self.gis_res.temperatureRegulation.value = 2
        sleep(1)
        self.assertEqual(self.gis_res.temperatureRegulation.value, 2)
        self.assertFalse(self.gis_res._gis.RegulationOn.Target.lower() == "true")
        self.assertTrue(self.gis_res._gis.RegulationRushOn.Target.lower() == "true")
        self.gis_res.temperatureRegulation.value = 0

    def test_temperatureRegulation(self):
        """
        Test if temperature regulation is functioning properly
        TODO: Tune the target temperatures test_value1 and test_value2 to reasonable values
              Tune the sleeping time so it waits long enough for the temperature to be reached
              Tune the test_accuracy (number of decimal places) to reflect the accuracy of the temperature regulation
        """
        test_value1 = 30  # TODO: Tune!
        test_value2 = 40  # TODO: Tune!
        test_accuracy = 0  # TODO: Tune!
        self.gis_res.temperatureTarget.value = test_value1
        self.gis_res.temperatureRegulation.value = 2
        if not TEST_NOHW == "sim":
            sleep(10)  # TODO: Tune!
        else:
            sleep(1)
        self.assertAlmostEqual(self.gis_res.temperature.value, test_value1, places=test_accuracy)

        self.gis_res.temperatureRegulation.value = 1
        self.gis_res.temperatureTarget.value = test_value2
        if not TEST_NOHW == "sim":
            sleep(10)  # TODO: Tune!
        else:
            sleep(1)
        self.assertAlmostEqual(self.gis_res.temperature.value, test_value2, places=test_accuracy)

        self.gis_res.temperatureRegulation.value = 0

    def test_stop(self):
        """
        Tests that calling stop has the expected behaviour
        """
        if not TEST_NOHW == 0:
            self.skipTest("No hardware present to test stop function.")

        try:
            self.gis_res.temperatureRegulation.value = 1
            sleep(1)
            self.gis.stop()
            self.assertEqual(self.gis_res.temperatureRegulation.value, 0)

            self.gis_res.temperatureRegulation.value = 2
            sleep(1)
            self.gis.stop()
            self.assertEqual(self.gis_res.temperatureRegulation.value, 0)

        except NameError:
            self.skipTest("No GIS to call stop on.")


class TestTestDevice(unittest.TestCase):
    """
    Tests for the test device
    """

    oserver = None

    @classmethod
    def setUpClass(cls):
        """
        Setup the Orsay client
        """
        cls.oserver = orsay.OrsayComponent(**CONFIG_ORSAY_TEST)

        cls.datamodel = cls.oserver.datamodel

        for child in cls.oserver.children.value:
            if child.name == CONFIG_TEST["name"]:
                cls.test_dev = child

    @classmethod
    def tearDownClass(cls):
        """
        Terminate the Orsay client
        """
        cls.oserver.terminate()

    def test_OrsayBooleanConnector(self):
        """
        Test the boolean VA TurboPump1.IsOn
        """
        self.datamodel.Scanner.OperatingMode.Target = 0
        sleep(1)
        print("Parameter: %s" % self.datamodel.Scanner.OperatingMode.Actual)
        print("VA: %s" % str(
            self.test_dev.OrsayBooleanConnector._VA_to_parameter_value(self.test_dev.testBooleanVA.value)))
        self.assertEqual(self.datamodel.Scanner.OperatingMode.Actual,
                         str(self.test_dev.OrsayBooleanConnector._VA_to_parameter_value(
                             self.test_dev.testBooleanVA.value)))

        self.datamodel.Scanner.OperatingMode.Target = 1
        sleep(1)
        print("Parameter: %s" % self.datamodel.Scanner.OperatingMode.Actual)
        print("VA: %s" % str(
            self.test_dev.OrsayBooleanConnector._VA_to_parameter_value(self.test_dev.testBooleanVA.value)))
        self.assertEqual(self.datamodel.Scanner.OperatingMode.Actual,
                         str(self.test_dev.OrsayBooleanConnector._VA_to_parameter_value(
                             self.test_dev.testBooleanVA.value)))

        self.test_dev.testBooleanVA.value = False
        sleep(1)
        print("Parameter: %s" % self.datamodel.Scanner.OperatingMode.Actual)
        print("VA: %s" % str(
            self.test_dev.OrsayBooleanConnector._VA_to_parameter_value(self.test_dev.testBooleanVA.value)))
        self.assertEqual(self.datamodel.Scanner.OperatingMode.Actual,
                         str(self.test_dev.OrsayBooleanConnector._VA_to_parameter_value(
                             self.test_dev.testBooleanVA.value)))

        self.test_dev.testBooleanVA.value = True
        sleep(1)
        print("Parameter: %s" % self.datamodel.Scanner.OperatingMode.Actual)
        print("VA: %s" % str(
            self.test_dev.OrsayBooleanConnector._VA_to_parameter_value(self.test_dev.testBooleanVA.value)))
        self.assertEqual(self.datamodel.Scanner.OperatingMode.Actual,
                         str(self.test_dev.OrsayBooleanConnector._VA_to_parameter_value(
                             self.test_dev.testBooleanVA.value)))

    def test_OrsayFloatConnector(self):
        """
        Test the float VA Manometer1.Pressure
        """
        self.datamodel.HybridPlatform.PumpingSystem.Manometer1.Pressure.Target = 0.1
        sleep(1)
        print("Parameter: %s" % self.datamodel.HybridPlatform.PumpingSystem.Manometer1.Pressure.Actual)
        print("VA: %s" % str(self.test_dev.testFloatVA.value))
        self.assertEqual(self.datamodel.HybridPlatform.PumpingSystem.Manometer1.Pressure.Actual,
                         str(self.test_dev.testFloatVA.value))

        self.datamodel.HybridPlatform.PumpingSystem.Manometer1.Pressure.Target = 0.2
        sleep(1)
        print("Parameter: %s" % self.datamodel.HybridPlatform.PumpingSystem.Manometer1.Pressure.Actual)
        print("VA: %s" % str(self.test_dev.testFloatVA.value))
        self.assertEqual(self.datamodel.HybridPlatform.PumpingSystem.Manometer1.Pressure.Actual,
                         str(self.test_dev.testFloatVA.value))

        self.test_dev.testFloatVA.value = 0.1
        sleep(1)
        print("Parameter: %s" % self.datamodel.HybridPlatform.PumpingSystem.Manometer1.Pressure.Actual)
        print("VA: %s" % str(self.test_dev.testFloatVA.value))
        self.assertEqual(self.datamodel.HybridPlatform.PumpingSystem.Manometer1.Pressure.Actual,
                         str(self.test_dev.testFloatVA.value))

        self.test_dev.testFloatVA.value = 0.2
        sleep(1)
        print("Parameter: %s" % self.datamodel.HybridPlatform.PumpingSystem.Manometer1.Pressure.Actual)
        print("VA: %s" % str(self.test_dev.testFloatVA.value))
        self.assertEqual(self.datamodel.HybridPlatform.PumpingSystem.Manometer1.Pressure.Actual,
                         str(self.test_dev.testFloatVA.value))

    def test_OrsayIntConnector(self):
        """
        Test the int VA HVPSFloatingIon.HeaterState
        """
        self.datamodel.HVPSFloatingIon.HeaterState.Target = 0
        sleep(1)
        print("Parameter: %s" % self.datamodel.HVPSFloatingIon.HeaterState.Actual)
        print("VA: %s" % str(self.test_dev.testIntVA.value))
        self.assertEqual(self.datamodel.HVPSFloatingIon.HeaterState.Actual, str(self.test_dev.testIntVA.value))

        self.datamodel.HVPSFloatingIon.HeaterState.Target = 1
        sleep(1)
        print("Parameter: %s" % self.datamodel.HVPSFloatingIon.HeaterState.Actual)
        print("VA: %s" % str(self.test_dev.testIntVA.value))
        self.assertEqual(self.datamodel.HVPSFloatingIon.HeaterState.Actual, str(self.test_dev.testIntVA.value))

        self.test_dev.testIntVA.value = 0
        sleep(1)
        print("Parameter: %s" % self.datamodel.HVPSFloatingIon.HeaterState.Actual)
        print("VA: %s" % str(self.test_dev.testIntVA.value))
        self.assertEqual(self.datamodel.HVPSFloatingIon.HeaterState.Actual, str(self.test_dev.testIntVA.value))

        self.test_dev.testIntVA.value = 1
        sleep(1)
        print("Parameter: %s" % self.datamodel.HVPSFloatingIon.HeaterState.Actual)
        print("VA: %s" % str(self.test_dev.testIntVA.value))
        self.assertEqual(self.datamodel.HVPSFloatingIon.HeaterState.Actual, str(self.test_dev.testIntVA.value))

    def test_OrsayTupleConnector(self):
        """
        Test the tuple VA IonColumnMCS.CondensorSteerer1StigmatorX and IonColumnMCS.CondensorSteerer1StigmatorY
        """
        self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorX.Target = 0.3
        sleep(1)
        print("Parameter: %s" % self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorX.Actual)
        print("VA: %s" % str(self.test_dev.testTupleVA.value[0]))
        self.assertEqual(self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorX.Actual,
                         str(self.test_dev.testTupleVA.value[0]))

        self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorX.Target = 0.4
        sleep(1)
        print("Parameter: %s" % self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorX.Actual)
        print("VA: %s" % str(self.test_dev.testTupleVA.value[0]))
        self.assertEqual(self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorX.Actual,
                         str(self.test_dev.testTupleVA.value[0]))

        self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorY.Target = 0.3
        sleep(1)
        print("Parameter: %s" % self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorY.Actual)
        print("VA: %s" % str(self.test_dev.testTupleVA.value[1]))
        self.assertEqual(self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorY.Actual,
                         str(self.test_dev.testTupleVA.value[1]))

        self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorY.Target = 0.4
        sleep(1)
        print("Parameter: %s" % self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorY.Actual)
        print("VA: %s" % str(self.test_dev.testTupleVA.value[1]))
        self.assertEqual(self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorY.Actual,
                         str(self.test_dev.testTupleVA.value[1]))

        self.test_dev.testTupleVA.value = (0.5, 0.6)
        sleep(1)
        print("Parameter X: %s" % self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorX.Actual)
        print("Parameter Y: %s" % self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorY.Actual)
        print("VA: %s" % str(self.test_dev.testTupleVA.value))
        self.assertEqual(self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorX.Actual,
                         str(self.test_dev.testTupleVA.value[0]))
        self.assertEqual(self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorY.Actual,
                         str(self.test_dev.testTupleVA.value[1]))

        self.test_dev.testTupleVA.value = (0.7, 0.8)
        sleep(1)
        print("Parameter X: %s" % self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorX.Actual)
        print("Parameter Y: %s" % self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorY.Actual)
        print("VA: %s" % str(self.test_dev.testTupleVA.value))
        self.assertEqual(self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorX.Actual,
                         str(self.test_dev.testTupleVA.value[0]))
        self.assertEqual(self.datamodel.IonColumnMCS.CondensorSteerer1StigmatorY.Actual,
                         str(self.test_dev.testTupleVA.value[1]))


class TestOrsayParameterConnector(unittest.TestCase):
    """
    Tests for the OrsayParameterConnector, to check if it properly raises exceptions when it should
    """

    oserver = None

    @classmethod
    def setUpClass(cls):
        """
        Setup the Orsay client
        """
        if TEST_NOHW == 1:
            raise unittest.SkipTest("TEST_NOHW is set. No server to contact.")

        cls.oserver = orsay.OrsayComponent(**CONFIG_ORSAY)
        cls.datamodel = cls.oserver.datamodel

    @classmethod
    def tearDownClass(cls):
        """
        Terminate the Orsay client
        """
        cls.oserver.terminate()

    def test_not_enough_parameters(self):
        with self.assertRaises(ValueError):  # no parameters passed
            orsay.OrsayParameterConnector(model.TupleVA((0, 0)), [])
        with self.assertRaises(ValueError):  # Length of Tuple VA does not match number of parameters passed
            orsay.OrsayParameterConnector(model.TupleVA((0, 0)), self.datamodel.HybridValveFIB.ErrorState)

    def test_no_tuple_va(self):
        with self.assertRaises(ValueError):  # Multiple parameters are passed, but VA is not of a tuple type
            orsay.OrsayParameterConnector(model.IntVA(0), [self.datamodel.HybridValveFIB.ErrorState,
                                                           self.datamodel.HybridIonPumpGunFIB.ErrorState])

    def test_not_connected(self):
        connector = orsay.OrsayParameterConnector(model.FloatVA(0.0), self.datamodel.HybridIonPumpGunFIB.Pressure)
        connector.disconnect()
        with self.assertRaises(AttributeError):  # OrsayParameterConnector is not connected to an Orsay parameter
            connector.update_VA()
        with self.assertRaises(AttributeError):  # OrsayParameterConnector is not connected to an Orsay parameter
            connector._update_parameter(1.0)

    def test_incorrect_parameter(self):
        connector = orsay.OrsayParameterConnector(model.FloatVA(0.0), self.datamodel.HybridIonPumpGunFIB.Pressure)
        with self.assertRaises(ValueError):  # Incorrect parameter passed
            connector.update_VA(parameter=self.datamodel.HybridIonPumpGunFIB.ErrorState)

    def test_readonly(self):
        connector = orsay.OrsayParameterConnector(model.FloatVA(0.0, readonly=True),
                                                  self.datamodel.HybridIonPumpGunFIB.Pressure)
        with self.assertRaises(model.NotSettableError):  # Value is read-only
            connector._update_parameter(1.0)


class TestFIBSource(unittest.TestCase):
    """
    Tests for the Focused Ion Beam (FIB) Source
    TODO: Tune the settletime of the hardware safe tests to values appropariate for the hardware
    """

    oserver = None

    @classmethod
    def setUpClass(cls):
        """
        Setup the Orsay client
        """
        if TEST_NOHW == 1:
            raise unittest.SkipTest("TEST_NOHW is set. No server to contact.")

        cls.oserver = orsay.OrsayComponent(**CONFIG_ORSAY)
        cls.datamodel = cls.oserver.datamodel
        for child in cls.oserver.children.value:
            if child.name == CONFIG_FIBSOURCE["name"]:
                cls.fib_source = child

    @classmethod
    def tearDownClass(cls):
        """
        Terminate the Orsay client
        """
        cls.oserver.terminate()

    def test_errorstate(self):
        """Check that any text in an ErrorState parameter results in that text in the state VA"""
        with self.assertRaises(ValueError):
            self.fib_source._updateErrorState(self.datamodel.HybridPlatform.Cancel)

        if not TEST_NOHW == "sim":
            self.skipTest("This test is not hardware safe.")

        test_string = "This thing broke"

        self.datamodel.HybridGaugeCompressedAir.ErrorState.Actual = test_string
        sleep(1)
        self.assertIsInstance(self.fib_source.state.value, HwError)
        self.assertIn("HybridGaugeCompressedAir", str(self.fib_source.state.value))
        self.assertIn(test_string, str(self.fib_source.state.value))
        self.datamodel.HybridGaugeCompressedAir.ErrorState.Actual = ""

        self.datamodel.HybridInterlockInChamberVac.ErrorState.Actual = test_string
        sleep(1)
        self.assertIsInstance(self.fib_source.state.value, HwError)
        self.assertIn("HybridInterlockInChamberVac", str(self.fib_source.state.value))
        self.assertIn(test_string, str(self.fib_source.state.value))
        self.datamodel.HybridInterlockInChamberVac.ErrorState.Actual = ""

        self.datamodel.HybridInterlockOutHVPS.ErrorState.Actual = test_string
        sleep(1)
        self.assertIsInstance(self.fib_source.state.value, HwError)
        self.assertIn("HybridInterlockOutHVPS", str(self.fib_source.state.value))
        self.assertIn(test_string, str(self.fib_source.state.value))
        self.datamodel.HybridInterlockOutHVPS.ErrorState.Actual = ""

        self.datamodel.HybridIonPumpColumnFIB.ErrorState.Actual = test_string
        sleep(1)
        self.assertIsInstance(self.fib_source.state.value, HwError)
        self.assertIn("HybridIonPumpColumnFIB", str(self.fib_source.state.value))
        self.assertIn(test_string, str(self.fib_source.state.value))
        self.datamodel.HybridIonPumpColumnFIB.ErrorState.Actual = ""

        self.datamodel.HybridIonPumpGunFIB.ErrorState.Actual = test_string
        sleep(1)
        self.assertIsInstance(self.fib_source.state.value, HwError)
        self.assertIn("HybridIonPumpGunFIB", str(self.fib_source.state.value))
        self.assertIn(test_string, str(self.fib_source.state.value))
        self.datamodel.HybridIonPumpGunFIB.ErrorState.Actual = ""

        self.datamodel.HybridValveFIB.ErrorState.Actual = test_string
        sleep(1)
        self.assertIsInstance(self.fib_source.state.value, HwError)
        self.assertIn("HybridValveFIB", str(self.fib_source.state.value))
        self.assertIn(test_string, str(self.fib_source.state.value))
        self.datamodel.HybridValveFIB.ErrorState.Actual = ""

        sleep(1)
        self.assertEqual(self.fib_source.state.value, model.ST_RUNNING)

    def test_interlockTriggered(self):
        """Check that the interlockTriggered VA is updated correctly"""
        with self.assertRaises(ValueError):
            self.fib_source._updateInterlockTriggered(self.datamodel.HybridPlatform.Cancel)

        connector_test(self, self.fib_source.interlockTriggered, self.fib_source._interlockHVPS.ErrorState,
                       [(True, orsay.INTERLOCK_DETECTED_STR), (False, "")],
                       readonly=True)
        connector_test(self, self.fib_source.interlockTriggered, self.fib_source._interlockChamber.ErrorState,
                       [(True, orsay.INTERLOCK_DETECTED_STR), (False, "")],
                       readonly=True)

    def test_gunPumpOn(self):
        """Check that the gunPumpOn VA is updated correctly"""
        connector_test(self, self.fib_source.gunPumpOn, self.fib_source._gunPump.IsOn,
                       [(True, "True"), (False, "False")], hw_safe=True, settletime=1)  # TODO: Tune the settle time

    def test_columnPumpOn(self):
        """Check that the columnPumpOn VA is updated correctly"""
        connector_test(self, self.fib_source.columnPumpOn, self.fib_source._columnPump.IsOn,
                       [(True, "True"), (False, "False")], hw_safe=True, settletime=1)  # TODO: Tune the settle time

    def test_gunOn(self):
        """Check that the gunOn VA is updated correctly"""
        self.fib_source.gunPumpOn.value = True  # pumps need to be on for the gun to be able to turn on
        self.fib_source.columnPumpOn.value = True
        connector_test(self, self.fib_source.gunOn, self.fib_source._hvps.GunState,
                       [(True, "ON"), (False, "OFF")], hw_safe=True, settletime=1)  # TODO: Tune the settle time
        self.fib_source.gunPumpOn.value = False
        self.fib_source.columnPumpOn.value = False

    def test_gunPressure(self):
        """Check that the gunPressure VA is updated correctly"""
        connector_test(self, self.fib_source.gunPressure, self.fib_source._gunPump.Pressure,
                       [(1e-3, 1e-3), (2e-3, 2e-3)], readonly=True)

    def test_columnPressure(self):
        """Check that the columnPressure VA is updated correctly"""
        connector_test(self, self.fib_source.columnPressure, self.fib_source._columnPump.Pressure,
                       [(1e-3, 1e-3), (2e-3, 2e-3)], readonly=True)

    def test_lifetime(self):
        """Check that the lifetime VA is updated correctly"""
        connector_test(self, self.fib_source.lifetime, self.fib_source._hvps.SourceLifeTime,
                       [(0.1, 0.1), (0.2, 0.2)], readonly=True)

    def test_currentRegulation(self):
        """Check that the currentRegulation VA is updated correctly"""
        connector_test(self, self.fib_source.currentRegulation, self.fib_source._hvps.BeamCurrent_Enabled,
                       [(True, "True"), (False, "False")], hw_safe=True, settletime=1)  # TODO: Tune the settle time

    def test_sourceCurrent(self):
        """Check that the sourceCurrent VA is updated correctly"""
        connector_test(self, self.fib_source.sourceCurrent, self.fib_source._hvps.BeamCurrent,
                       [(1e-5, 1e-5), (0, 0)], readonly=True)

    def test_suppressorVoltage(self):
        """Check that the suppressorVoltage VA is updated correctly"""
        connector_test(self, self.fib_source.suppressorVoltage, self.fib_source._hvps.Suppressor,
                       [(10, 10), (0, 0)], hw_safe=True, settletime=1)  # TODO: Tune the settle time

    def test_heatingCurrent(self):
        """Check that the heatingCurrent VA is updated correctly"""
        connector_test(self, self.fib_source.heatingCurrent, self.fib_source._hvps.Heater,
                       [(1, 1), (0, 0)], hw_safe=True, settletime=1)  # TODO: Tune the settle time

    # def test_heaterState(self):
    #     """Check that the heaterState VA is updated correctly"""
    #     connector_test(self, self.fib_source.heaterState, self.fib_source._hvps.HeaterState,
    #                    [(1, 1), (0, 0)], hw_safe=True)

    def test_acceleratorVoltage(self):
        """Check that the heaterState VA is updated correctly"""
        connector_test(self, self.fib_source.acceleratorVoltage, self.fib_source._hvps.Energy,
                       [(10, 10), (0, 0)], hw_safe=True, settletime=1)  # TODO: Tune the settle time

    def test_energyLink(self):
        """Check that the energyLink VA is updated correctly"""
        connector_test(self, self.fib_source.energyLink, self.fib_source._hvps.EnergyLink,
                       [(True, "ON"), (False, "OFF")], hw_safe=True, settletime=1)  # TODO: Tune the settle time

    def test_extractorVoltage(self):
        """Check that the extractorVoltage VA is updated correctly"""
        connector_test(self, self.fib_source.extractorVoltage, self.fib_source._hvps.Extractor,
                       [(10, 10), (0, 0)], hw_safe=True, settletime=1)  # TODO: Tune the settle time


def connector_test(test_case, va, parameter, valuepairs, readonly=False, hw_safe=False, settletime=1):
    """
    Standard test for testing an OrsayParameterConnector.
    :param test_case: is the TestCase class this test is a part of
    :param va: is the VA to test with.
    :param parameter: is the parameter that should be connected to the va.
    :param valuepairs: is a list of tuples. Each tuple should contain two values. The first is a value the va could
        have, the second is the corresponding value of the parameter. For a good test, supply at least two pairs.
    :param readonly: tells the test if the va is readonly or can be written to. If readonly is True, only communication
        from the Orsay server to the va is tested. Otherwise two way communication is tested. If readonly is True, the
        test will not be performed on the real hardware, because we cannot write to the parameter's Actual value, as
        we'd want to test the reading. Defaults to False.
    :param hw_safe: tells the test if this it is safe to perform this test on the real hardware. Defaults to False.
    :param settletime: is the time the test will wait between setting the Target of the Orsay parameter and checking if
        the Actual value of the Orsay parameter matches the VA's value. In simulation, this value is overwritten by 1.
        Defaults to 1.
    :returns: Nothing
    """
    if TEST_NOHW == 1:
        test_case.skipTest("TEST_NOHW is set. No server to contact.")

    if not TEST_NOHW == "sim" and not hw_safe:
        test_case.skipTest("This test is not hardware safe.")

    if len(valuepairs) < 2:
        logging.warning("Less than 2 value pairs supplied for testing. Test may return a false positive.")

    attribute = "Target"
    if TEST_NOHW == "sim":
        attribute = "Actual"
        settletime = 1

    for (va_value, par_value) in valuepairs:
        setattr(parameter, attribute, par_value)
        sleep(settletime)
        test_case.assertEqual(va.value, va_value)

    if not readonly:
        for (va_value, par_value) in valuepairs:  # loop twice to assure value pairs are alternated
            va.value = va_value
            sleep(1)
            target = type(par_value)(parameter.Target)  # needed since many parameter values are strings
            test_case.assertEqual(target, par_value)


if __name__ == '__main__':
    unittest.main()
