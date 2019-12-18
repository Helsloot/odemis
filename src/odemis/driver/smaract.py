# -*- coding: utf-8 -*-
'''
Created on 17 April 2019

@author: Anders Muskens

Copyright © 2012-2019 Anders Muskens, Delmic

This file is part of Odemis.

Odemis is free software: you can redistribute it and/or modify it under the terms
of the GNU General Public License version 2 as published by the Free Software
Foundation.

Odemis is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR
PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
Odemis. If not, see http://www.gnu.org/licenses/.

This driver supports SmarAct and SmarPod actuators, which are accessed via a C DLL library
provided by SmarAct. This must be installed on the system for this actuator to run. Please
refer to the SmarAct readme for Linux installation instructions.
'''
from __future__ import division
from concurrent.futures import CancelledError, TimeoutError

import os
import logging
import time
import math
from ctypes import *
import copy
import threading

from odemis import model
from odemis.util import driver
from odemis.model import CancellableFuture, CancellableThreadPoolExecutor, isasync


def add_coord(pos1, pos2):
    """
    Adds two coordinate dictionaries together and returns a new coordinate dictionary.

    All of the keys (axis names) in pos2 must be present in pos1

    pos1: dict (axis name str) -> (float)
    pos2: dict (axis name str) -> (float)
    Returns ret
        dict (axis name str) -> (float)
    """
    ret = pos1.copy()
    for an, v in pos2.items():
        ret[an] += v

    return ret


class Pose(Structure):
    """
    SmarPod Pose Structure (C Struct used by DLL)

    Note: internally, the system uses metres and degrees for rotation
    """
    _fields_ = [
        ("positionX", c_double),
        ("positionY", c_double),
        ("positionZ", c_double),
        ("rotationX", c_double),
        ("rotationY", c_double),
        ("rotationZ", c_double),
        ]
    
    def __add__(self, o):
        pose = Pose()
        pose.positionX = self.positionX + o.positionX
        pose.positionY = self.positionY + o.positionY
        pose.positionZ = self.positionZ + o.positionZ
        pose.rotationX = self.rotationX + o.rotationX
        pose.rotationY = self.rotationY + o.rotationY
        pose.rotationZ = self.rotationZ + o.rotationZ
        return pose

    def __sub__(self, o):
        pose = Pose()
        pose.positionX = self.positionX - o.positionX
        pose.positionY = self.positionY - o.positionY
        pose.positionZ = self.positionZ - o.positionZ
        pose.rotationX = self.rotationX - o.rotationX
        pose.rotationY = self.rotationY - o.rotationY
        pose.rotationZ = self.rotationZ - o.rotationZ
        return pose


def pose_to_dict(pose):
    """
    Convert a Pose (C structure) to a coordinate dictionary (str) -> (double)
        pose: a Pose (C struct)
    returns: Coordinate dictionary (str) -> (double) of axis name to value
    """
    pos = {}
    pos['x'] = pose.positionX
    pos['y'] = pose.positionY
    pos['z'] = pose.positionZ

    # Note: internally, the system uses metres and degrees for rotation
    pos['rx'] = math.radians(pose.rotationX)
    pos['ry'] = math.radians(pose.rotationY)
    pos['rz'] = math.radians(pose.rotationZ)
    return pos


def dict_to_pose(pos, base=Pose()):
    """
    Convert a coordinate dictionary (str) -> (double) to a Pose C struct
        pos: Coordinate dictionary (str) -> (double) of axis name to value
        base: a Pose that is used as a base in case you don't want to initialize
            the C struct to unknown values in the case where not all axes
            are defined in the pos dict.
    returns: a Pose (C struct)
    raises ValueError if an unsupported axis name is input
    """

    # Note: internally, the system uses metres and degrees for rotation
    for an, v in pos.items():
        if an == "x":
            base.positionX = v
        elif an == "y":
            base.positionY = v
        elif an == "z":
            base.positionZ = v
        elif an == "rx":
            base.rotationX = math.degrees(v)
        elif an == "ry":
            base.rotationY = math.degrees(v)
        elif an == "rz":
            base.rotationZ = math.degrees(v)
        else:
            raise ValueError("Invalid axis")
    return base


class SmarPodDLL(CDLL):
    """
    Subclass of CDLL specific to SmarPod library, which handles error codes for
    all the functions automatically.
    """
    
    hwModel = c_long(10001)  # specifies the SmarPod 110.45 S (nano)

    # Status
    SMARPOD_OK = 0
    SMARPOD_OTHER_ERROR = 1
    SMARPOD_SYSTEM_NOT_INITIALIZED_ERROR = 2
    SMARPOD_NO_SYSTEMS_FOUND_ERROR = 3
    SMARPOD_INVALID_PARAMETER_ERROR = 4
    SMARPOD_COMMUNICATION_ERROR = 5
    SMARPOD_UNKNOWN_PROPERTY_ERROR = 6
    SMARPOD_RESOURCE_TOO_OLD_ERROR = 7
    SMARPOD_FEATURE_UNAVAILABLE_ERROR = 8
    SMARPOD_INVALID_SYSTEM_LOCATOR_ERROR = 9
    SMARPOD_QUERYBUFFER_SIZE_ERROR = 10
    SMARPOD_COMMUNICATION_TIMEOUT_ERROR = 11
    SMARPOD_DRIVER_ERROR = 12
    SMARPOD_STATUS_CODE_UNKNOWN_ERROR = 500
    SMARPOD_INVALID_ID_ERROR = 501
    SMARPOD_INITIALIZED_ERROR = 502
    SMARPOD_HARDWARE_MODEL_UNKNOWN_ERROR = 503
    SMARPOD_WRONG_COMM_MODE_ERROR = 504
    SMARPOD_NOT_INITIALIZED_ERROR = 505
    SMARPOD_INVALID_SYSTEM_ID_ERROR = 506
    SMARPOD_NOT_ENOUGH_CHANNELS_ERROR = 507
    SMARPOD_INVALID_CHANNEL_ERROR = 508
    SMARPOD_CHANNEL_USED_ERROR = 509
    SMARPOD_SENSORS_DISABLED_ERROR = 510
    SMARPOD_WRONG_SENSOR_TYPE_ERROR = 511
    SMARPOD_SYSTEM_CONFIGURATION_ERROR = 512
    SMARPOD_SENSOR_NOT_FOUND_ERROR = 513
    SMARPOD_STOPPED_ERROR = 514
    SMARPOD_BUSY_ERROR = 515
    SMARPOD_NOT_REFERENCED_ERROR = 550
    SMARPOD_POSE_UNREACHABLE_ERROR = 551

    # Defines
    SMARPOD_SENSORS_DISABLED = c_uint(0)
    SMARPOD_SENSORS_ENABLED = c_uint(1)
    SMARPOD_SENSORS_POWERSAVE = c_uint(2)

    # property symbols
    SMARPOD_FREF_METHOD = c_uint(1000)
    SMARPOD_FREF_ZDIRECTION = c_uint(1002)
    SMARPOD_FREF_XDIRECTION = c_uint(1003)
    SMARPOD_FREF_YDIRECTION = c_uint(1004)
    SMARPOD_PIVOT_MODE = c_uint(1010)
    SMARPOD_FREF_AND_CAL_FREQUENCY = c_uint(1020)
    SMARPOD_POSITIONERS_MIN_SPEED = c_uint(1100)

    # move-status constants
    SMARPOD_STOPPED = c_uint(0)
    SMARPOD_HOLDING = c_uint(1)
    SMARPOD_MOVING = c_uint(2)
    SMARPOD_CALIBRATING = c_uint(3)
    SMARPOD_REFERENCING = c_uint(4)
    SMARPOD_STANDBY = c_uint(5)
    
    SMARPOD_HOLDTIME_INFINITE = c_uint(60000)

    err_code = {
0: "OK",
1: "OTHER_ERROR",
2: "SYSTEM_NOT_INITIALIZED_ERROR",
3: "NO_SYSTEMS_FOUND_ERROR",
4: "INVALID_PARAMETER_ERROR",
5: "COMMUNICATION_ERROR",
6: "UNKNOWN_PROPERTY_ERROR",
7: "RESOURCE_TOO_OLD_ERROR",
8: "FEATURE_UNAVAILABLE_ERROR",
9: "INVALID_SYSTEM_LOCATOR_ERROR",
10: "QUERYBUFFER_SIZE_ERROR",
11: "COMMUNICATION_TIMEOUT_ERROR",
12: "DRIVER_ERROR",
500: "STATUS_CODE_UNKNOWN_ERROR",
501: "INVALID_ID_ERROR",
503: "HARDWARE_MODEL_UNKNOWN_ERROR",
504: "WRONG_COMM_MODE_ERROR",
505: "NOT_INITIALIZED_ERROR",
506: "INVALID_SYSTEM_ID_ERROR",
507: "NOT_ENOUGH_CHANNELS_ERROR",
510: "SENSORS_DISABLED_ERROR",
511: "WRONG_SENSOR_TYPE_ERROR",
512: "SYSTEM_CONFIGURATION_ERROR",
513: "SENSOR_NOT_FOUND_ERROR",
514: "STOPPED_ERROR",
515: "BUSY_ERROR",
550: "NOT_REFERENCED_ERROR",
551: "POSE_UNREACHABLE_ERROR",
552: "COMMAND_OVERRIDDEN_ERROR",
553: "ENDSTOP_REACHED_ERROR",
554: "NOT_STOPPED_ERROR",
555: "COULD_NOT_REFERENCE_ERROR",
556: "COULD_NOT_CALIBRATE_ERROR",
        }

    def __init__(self):
        if os.name == "nt":
            raise NotImplemented("Windows not yet supported")
            # WinDLL.__init__(self, "libsmarpod.dll")  # TODO check it works
            # atmcd64d.dll on 64 bits
        else:
            # Global so that its sub-libraries can access it
            CDLL.__init__(self, "libsmarpod.so", RTLD_GLOBAL)

    def __getitem__(self, name):
        try:
            func = super(SmarPodDLL, self).__getitem__(name)
        except Exception:
            raise AttributeError("Failed to find %s" % (name,))
        func.__name__ = name
        func.errcheck = self.sp_errcheck
        return func

    @staticmethod
    def sp_errcheck(result, func, args):
        """
        Analyse the retuhwModelrn value of a call and raise an exception in case of
        error.
        Follows the ctypes.errcheck callback convention
        """
        if result != SmarPodDLL.SMARPOD_OK:
            raise SmarPodError(result)

        return result


class SmarPodError(Exception):
    """
    SmarPod Exception
    """
    def __init__(self, error_code):
        self.errno = error_code
        super(SmarPodError, self).__init__("Error %d. %s" % (error_code, SmarPodDLL.err_code.get(error_code, "")))


class SmarPod(model.Actuator):
    
    def __init__(self, name, role, locator, ref_on_init=False, actuator_speed=0.1,
                 axes=None, **kwargs):
        """
        A driver for a SmarAct SmarPod Actuator.
        This driver uses a DLL provided by SmarAct which connects via
        USB or TCP/IP using a locator string.

        name: (str)
        role: (str)
        locator: (str) Use "fake" for a simulator.
            For a real device, MCS controllers with USB interface can be addressed with the
            following locator syntax:
                usb:id:<id>
            where <id> is the first part of a USB devices serial number which
            is printed on the MCS controller.
            If the controller has a TCP/IP connection, use:
                network:<ip>:<port>
        ref_on_init: (bool) determines if the controller should automatically reference
            on initialization
        actuator_speed: (double) the default speed (in m/s) of the actuators
        axes: dict str (axis name) -> dict (axis parameters)
            axis parameters: {
                range: [float, float], default is -1 -> 1
                unit: (str) default will be set to 'm'
            }
        """
        if not axes:
            raise ValueError("Needs at least 1 axis.")

        if locator != "fake":
            self.core = SmarPodDLL()
        else:
            self.core = FakeSmarPodDLL()
            
        # Not to be mistaken with axes which is a simple public view
        self._axis_map = {}  # axis name -> axis number used by controller
        axes_def = {}  # axis name -> Axis object
        self._locator = c_char_p(locator.encode("ascii"))
        self._options = c_char_p("".encode("ascii"))  # In the current version, this must be an empty string.

        for axis_name, axis_par in axes.items():
            try:
                axis_range = axis_par['range']
            except KeyError:
                logging.info("Axis %s has no range. Assuming (-1, 1)", axis_name)
                axis_range = (-1, 1)

            try:
                axis_unit = axis_par['unit']
            except KeyError:
                logging.info("Axis %s has no unit. Assuming m", axis_name)
                axis_unit = "m"

            ad = model.Axis(canAbs=True, unit=axis_unit, range=axis_range)
            axes_def[axis_name] = ad
            
            
        # Connect to the device
        self._id = c_uint()
        self.core.Smarpod_Open(byref(self._id), SmarPodDLL.hwModel, self._locator, self._options)
        logging.debug("Successfully connected to SmarPod Controller ID %d", self._id.value)
        self.core.Smarpod_SetSensorMode(self._id, SmarPodDLL.SMARPOD_SENSORS_ENABLED)

        model.Actuator.__init__(self, name, role, axes=axes_def, **kwargs)

        # Add metadata
        self._swVersion = self.GetSwVersion()
        self._metadata[model.MD_SW_VERSION] = self._swVersion
        logging.debug("Using SmarPod library version %s", self._swVersion)

        self.position = model.VigilantAttribute({}, readonly=True)

        # will take care of executing axis move asynchronously
        self._executor = CancellableThreadPoolExecutor(1)  # one task at a time

        referenced = c_int()
        self.core.Smarpod_IsReferenced(self._id, byref(referenced))
        # define the referenced VA from the query
        axes_ref = {a: referenced.value for a, i in self.axes.items()}
        # VA dict str(axis) -> bool
        self.referenced = model.VigilantAttribute(axes_ref, readonly=True)
        # If ref_on_init, referenced immediately.
        if referenced.value:
            logging.debug("SmarPod is referenced")
        else:
            logging.warning("SmarPod is not referenced. The device will not function until referencing occurs.")
            if ref_on_init:
                self.reference().result()

        # Use a default actuator speed
        self._set_speed(actuator_speed)
        self._speed = self._get_speed()
        self._accel = self.GetAcceleration()

        self._updatePosition()

    def terminate(self):
        # should be safe to close the device multiple times if terminate is called more than once.
        self.core.Smarpod_Close(self._id)
        super(SmarPod, self).terminate()
        
    def GetSwVersion(self):
        """
        Request the software version from the DLL file
        """
        major = c_uint()
        minor = c_uint()
        update = c_uint()
        self.core.Smarpod_GetDLLVersion(byref(major), byref(minor), byref(update))
        ver = "%u.%u.%u" % (major.value, minor.value, update.value)
        return ver

    def _is_referenced(self):
        """
        Ask the controller if it is referenced
        """
        referenced = c_int()
        self.core.Smarpod_IsReferenced(self._id, byref(referenced))
        return bool(referenced.value)

    def GetMoveStatus(self):
        """
        Gets the move status from the controller.
        Returns:
            SmarPodDLL.SMARPOD_MOVING is returned if moving
            SmarPodDLL.SMARPOD_STOPPED when stopped
            SmarPodDLL.SMARPOD_HOLDING when holding between moves
            SmarPodDLL.SMARPOD_CALIBRATING when calibrating
            SmarPodDLL.SMARPOD_REFERENCING when referencing
            SmarPodDLL.SMARPOD_STANDBY
        """
        status = c_uint()
        self.core.Smarpod_GetMoveStatus(self._id, byref(status))
        return status

    def Move(self, pos, hold_time=0, block=False):
        """
        Move to pose command.
        pos: (dict str -> float) axis name -> position
            This is converted to the pose C-struct which is sent to the SmarPod DLL
        hold_time: (float) specify in seconds how long to hold after the move.
            If set to float(inf), will hold forever until a stop command is issued.
        block: (bool) Set to True if the function should block until the move completes

        Raises: SmarPodError if a problem occurs
        """
        # convert into a smartpad pose
        newPose = dict_to_pose(pos, self.GetPose())

        if hold_time == float("inf"):
            ht = SmarPodDLL.SMARPOD_HOLDTIME_INFINITE
        else:
            ht = c_uint(int(hold_time * 1000.0))

        # Use an infiinite holdtime and non-blocking (final argument)
        self.core.Smarpod_Move(self._id, byref(newPose), ht, c_int(block))

    def GetPose(self):
        """
        Get the current pose of the SmarPod

        returns: (dict str -> float): axis name -> position
        """
        pose = Pose()
        self.core.Smarpod_GetPose(self._id, byref(pose))
        return pose

    def Stop(self):
        """
        Stop command sent to the SmarPod
        """
        logging.debug("Stopping...")
        self.core.Smarpod_Stop(self._id)

    def _set_speed(self, value):
        """
        Set the speed of the SmarPod motion
        value: (double) indicating speed for all axes
        """
        logging.debug("Setting speed to %f", value)
        # the second argument (1) turns on speed control.
        self.core.Smarpod_SetSpeed(self._id, c_int(1), c_double(value))

    def _get_speed(self):
        """
        Returns (double) the speed of the SmarPod motion
        """
        speed_control = c_int()
        speed = c_double()
        self.core.Smarpod_GetSpeed(self._id, byref(speed_control), byref(speed))
        return speed.value

    def SetAcceleration(self, value):
        """
        Set the acceleration of the SmarPod motion
        value: (double) indicating acceleration for all axes
        """
        logging.debug("Setting acceleration to %f", value)
        # Passing 1 enables acceleration control.
        self.core.Smarpod_SetAcceleration(self._id, c_int(1), c_double(value))

    def GetAcceleration(self):
        """
        Returns (double) the acceleration of the SmarPod motion
        """
        acceleration_control = c_int()
        acceleration = c_double()
        self.core.Smarpod_GetAcceleration(self._id, byref(acceleration_control), byref(acceleration))
        return acceleration.value
    
    def IsPoseReachable(self, pos):
        """
        Ask the controller if a pose is reachable
        pos: (dict of str -> float): a coordinate dictionary of axis name to value
        returns: true if the pose is reachable - false otherwise.
        """
        reachable = c_int()
        self.core.Smarpod_IsPoseReachable(self._id, byref(dict_to_pose(pos)), byref(reachable))
        return bool(reachable.value)
    
    def stop(self, axes=None):
        """
        Stop the SmarPod controller and update position
        """
        self.Stop()
        self._updatePosition()

    def _updatePosition(self):
        """
        update the position VA
        """
        try:
            p = pose_to_dict(self.GetPose())
        except SmarPodError as ex:
            if ex.errno == SmarPodDLL.SMARPOD_NOT_REFERENCED_ERROR:
                logging.warning("Position unknown because SmarPod is not referenced")
                p = {'x': 0, 'y': 0, 'z': 0, 'rx': 0, 'ry': 0, 'rz': 0}
            else:
                raise

        p = self._applyInversion(p)
        logging.debug("Updated position to %s", p)
        self.position._set_value(p, force_write=True)

    def _createMoveFuture(self, ref=False):
        """
        ref: if true, will use a different canceller
        Return (CancellableFuture): a future that can be used to manage a move
        """
        f = CancellableFuture()
        f._moving_lock = threading.Lock()  # taken while moving
        f._must_stop = threading.Event()  # cancel of the current future requested
        f._was_stopped = False  # if cancel was successful
        if not ref:
            f.task_canceller = self._cancelCurrentMove
        else:
            f.task_canceller = self._cancelReference
        return f

    @isasync
    def reference(self, _=None):
        """
        reference usually takes axes as an argument. However, the SmarPod references all
        axes together so this argument is extraneous.
        """
        f = self._createMoveFuture(ref=True)
        self._executor.submitf(f, self._doReference, f)
        return f

    def _doReference(self, future):
        """
        Actually runs the referencing code
        future (Future): the future it handles
        raise:
            IOError: if referencing failed due to hardware
            CancelledError if was cancelled
        """
        # Reset reference so that if it fails, it states the axes are not
        # referenced (anymore)
        with future._moving_lock:
            try:
                # set the referencing for all axes to fals
                self.referenced._value = {a: False for a in self.axes.keys()}

                # The SmarPod references all axes at once. This function blocks
                self.core.Smarpod_FindReferenceMarks(self._id)

                if self._is_referenced():
                    self.referenced._value = {a: True for a in self.axes.keys()}
                    self._updatePosition()
                    logging.info("Referencing successful.")

            except SmarPodError as ex:
                future._was_stopped = True
                # This occurs if a stop command interrupts referencing
                if ex.errno == SmarPodDLL.SMARPOD_STOPPED_ERROR:
                    logging.info("Referencing stopped: %s", ex)
                    raise CancelledError()
                else:
                    raise
            except Exception:
                logging.exception("Referencing failure")
                raise
            finally:
                # We only notify after updating the position so that when a listener
                # receives updates both values are already updated.
                # read-only so manually notify
                self.referenced.notify(self.referenced.value)

    @isasync
    def moveAbs(self, pos):
        """
        API call to absolute move
        """
        if not pos:
            return model.InstantaneousFuture()
        
        self._checkMoveAbs(pos)
        if not self.IsPoseReachable(pos):
            raise ValueError("Pose %s is not reachable by the SmarPod controller" % (pos,))

        f = self._createMoveFuture()
        f = self._executor.submitf(f, self._doMoveAbs, f, pos)
        return f

    def _doMoveAbs(self, future, pos):
        """
        Blocking and cancellable absolute move
        future (Future): the future it handles
        _pos (dict str -> float): axis name -> absolute target position
        raise:
            SmarPodError: if the controller reported an error
            CancelledError: if cancelled before the end of the move
        """
        last_upd = time.time()
        dur = 30  # TODO: Calculate an estimated move duration
        end = time.time() + dur
        max_dur = dur * 2 + 1
        logging.debug("Expecting a move of %g s, will wait up to %g s", dur, max_dur)
        timeout = last_upd + max_dur

        with future._moving_lock:
            self.Move(pos)
            while not future._must_stop.is_set():
                status = self.GetMoveStatus()
                # check if move is done
                if status.value == SmarPodDLL.SMARPOD_STOPPED.value:
                    break

                now = time.time()
                if now > timeout:
                    logging.warning("Stopping move due to timeout after %g s.", max_dur)
                    self.stop()
                    raise TimeoutError("Move is not over after %g s, while "
                                       "expected it takes only %g s" %
                                       (max_dur, dur))

                # Update the position from time to time (10 Hz)
                if now - last_upd > 0.1:
                    self._updatePosition()
                    last_upd = time.time()

                # Wait half of the time left (maximum 0.1 s)
                left = end - time.time()
                sleept = max(0.001, min(left / 2, 0.1))
                future._must_stop.wait(sleept)
            else:
                self.stop()
                future._was_stopped = True
                raise CancelledError()

        self._updatePosition()

        logging.debug("move successfully completed")

    def _cancelCurrentMove(self, future):
        """
        Cancels the current move (both absolute or relative). Non-blocking.
        future (Future): the future to stop. Unused, only one future must be
         running at a time.
        return (bool): True if it successfully cancelled (stopped) the move.
        """
        # The difficulty is to synchronise correctly when:
        #  * the task is just starting (not finished requesting axes to move)
        #  * the task is finishing (about to say that it finished successfully)
        logging.debug("Canceling current move")

        future._must_stop.set()  # tell the thread taking care of the move it's over
        with future._moving_lock:
            if not future._was_stopped:
                logging.debug("Canceling failed")
            self._updatePosition()
            return future._was_stopped

    def _cancelReference(self, future):
        # The difficulty is to synchronize correctly when:
        #  * the task is just starting (about to request axes to move)
        #  * the task is finishing (about to say that it finished successfully)
        logging.debug("Canceling current referencing")

        self.Stop()
        future._must_stop.set()  # tell the thread taking care of the referencing it's over

        # Synchronise with the ending of the future
        with future._moving_lock:

            if not future._was_stopped:
                logging.debug("Cancelling failed")
            return future._was_stopped

    @isasync
    def moveRel(self, shift):
        """
        API call for relative move
        """
        if not shift:
            return model.InstantaneousFuture()

        self._checkMoveRel(shift)

        f = self._createMoveFuture()
        f = self._executor.submitf(f, self._doMoveRel, f, shift)
        return f
    
    def _doMoveRel(self, future, shift):
        """
        Do a relative move by converting it into an absolute move
        """
        pos = add_coord(self.position.value, shift)
        self._doMoveAbs(future, pos)

# Only for testing/simulation purpose
# Very rough version that is just enough so that if the wrapper behaves correctly,
# it returns the expected values.


def _deref(p, typep):
    """
    p (byref object)
    typep (c_type): type of pointer
    Use .value to change the value of the object
    """
    # This is using internal ctypes attributes, that might change in later
    # versions. Ugly!
    # Another possibility would be to redefine byref by identity function:
    # byref= lambda x: x
    # and then dereferencing would be also identity function.
    return typep.from_address(addressof(p._obj))


class FakeSmarPodDLL(object):
    """
    Fake SmarPod DLL for simulator
    """

    def __init__(self):
        self.pose = Pose()
        self.target = Pose()
        self.properties = {}
        self._speed = c_double(0)
        self._speed_control = c_int()
        self._accel_control = c_int()
        self._accel = c_double(0)
        self.referenced = False

        # Specify ranges
        self._range = {}
        self._range['x'] = (-1, 1)
        self._range['y'] = (-1, 1)
        self._range['z'] = (-1, 1)
        self._range['rx'] = (-45, 45)
        self._range['ry'] = (-45, 45)
        self._range['rz'] = (-45, 45)

        self.stopping = threading.Event()

        self._current_move_start = time.time()
        self._current_move_finish = time.time()

    def _pose_in_range(self, pose):
        if self._range['x'][0] <= pose.positionX <= self._range['x'][1] and \
            self._range['y'][0] <= pose.positionY <= self._range['y'][1] and \
            self._range['z'][0] <= pose.positionZ <= self._range['z'][1] and \
            self._range['rx'][0] <= pose.rotationX <= self._range['rx'][1] and \
            self._range['ry'][0] <= pose.rotationY <= self._range['ry'][1] and \
            self._range['rz'][0] <= pose.rotationZ <= self._range['rz'][1]:
            return True
        else:
            return False

    """
    DLL functions (fake)
    These functions are provided by the real SmarPod DLL
    """
    def Smarpod_Open(self, id, timeout, locator, options):
        pass

    def Smarpod_Close(self, id):
        pass

    def Smarpod_SetSensorMode(self, id, mode):
        pass

    def Smarpod_FindReferenceMarks(self, id):
        self.stopping.clear()
        time.sleep(0.5)
        if self.stopping.is_set():
            self.referenced = False
            raise SmarPodError(SmarPodDLL.SMARPOD_STOPPED_ERROR)
        else:
            self.referenced = True

    def Smarpod_IsPoseReachable(self, id, p_pos, p_reachable):
        reachable = _deref(p_reachable, c_int)
        pos = _deref(p_pos, Pose)
        if self._pose_in_range(pos):
            reachable.value = 1
        else:
            reachable.value = 0

    def Smarpod_IsReferenced(self, id, p_referenced):
        referenced = _deref(p_referenced, c_int)
        referenced.value = 1 if self.referenced else 0

    def Smarpod_Move(self, id, p_pose, hold_time, block):
        self.stopping.clear()
        pose = _deref(p_pose, Pose)
        if self._pose_in_range(pose):
            self._current_move_finish = time.time() + 1.0
            self.target.positionX = pose.positionX
            self.target.positionY = pose.positionY
            self.target.positionZ = pose.positionZ
            self.target.rotationX = pose.rotationX
            self.target.rotationY = pose.rotationY
            self.target.rotationZ = pose.rotationZ
        else:
            raise SmarPodError(SmarPodDLL.SMARPOD_POSE_UNREACHABLE_ERROR)

    def Smarpod_GetPose(self, id, p_pose):
        pose = _deref(p_pose, Pose)
        pose.positionX = self.pose.positionX
        pose.positionY = self.pose.positionY
        pose.positionZ = self.pose.positionZ
        pose.rotationX = self.pose.rotationX
        pose.rotationY = self.pose.rotationY
        pose.rotationZ = self.pose.rotationZ
        return SmarPodDLL.SMARPOD_OK

    def Smarpod_GetMoveStatus(self, id, p_status):
        status = _deref(p_status, c_int)

        if time.time() > self._current_move_finish:
            self.pose = copy.copy(self.target)
            status.value = SmarPodDLL.SMARPOD_STOPPED.value
        else:
            status.value = SmarPodDLL.SMARPOD_MOVING.value

    def Smarpod_Stop(self, id):
        self.stopping.set()

    def Smarpod_SetSpeed(self, id, speed_control, speed):
        self._speed = speed
        self._speed_control = speed_control

    def Smarpod_GetSpeed(self, id, p_speed_control, p_speed):
        speed = _deref(p_speed, c_double)
        speed.value = self._speed.value
        speed_control = _deref(p_speed_control, c_int)
        speed_control.value = self._speed_control.value

    def Smarpod_SetAcceleration(self, id, accel_control, accel):
        self._accel = accel
        self._accel_control = accel_control

    def Smarpod_GetAcceleration(self, id, p_accel_control, p_accel):
        accel = _deref(p_accel, c_double)
        accel.value = self._accel.value
        accel_control = _deref(p_accel_control, c_int)
        accel_control.value = self._accel_control.value

    def Smarpod_GetDLLVersion(self, p_major, p_minor, p_update):
        major = _deref(p_major, c_uint)
        major.value = 1
        minor = _deref(p_minor, c_uint)
        minor.value = 2
        update = _deref(p_update, c_uint)
        update.value = 3


"""
Classes associated with the SmarAct MC 5DOF Controller (custom for Delmic)
"""


class SA_MC_EventData(Union):
    """
    SA_MC event data is stored as this type of union (A C union used by DLL)
    """
    _fields_ = [
         ("i32", c_int32),
         ("i64", c_int64),
         ("reserved", c_int8 * 32),
         ]


class SA_MC_Event(Structure):
    """
    SA_MC Event structure (C struct used by DLL)
    """
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", c_uint32),
        ("unused", c_int8 * 28),
        ("u", SA_MC_EventData),
        ]


class SA_MC_Vec3(Structure):
    """
    SA_MC 3d vector Structure (C Struct used by DLL)
    """
    _fields_ = [
        ("x", c_double),
        ("y", c_double),
        ("z", c_double),
        ]


class SA_MC_Pose(Structure):
    """
    SA_MC Pose Structure (C Struct used by DLL)

    Note: internally, the system uses metres and degrees for rotation
    """
    _fields_ = [
        ("x", c_double),
        ("y", c_double),
        ("z", c_double),
        ("rx", c_double),
        ("ry", c_double),
        ("rz", c_double),
        ]

    def __add__(self, o):
        pose = SA_MC_Pose()
        pose.x = self.x + o.x
        pose.y = self.y + o.y
        pose.z = self.z + o.z
        pose.rx = self.rx + o.rx
        pose.ry = self.ry + o.ry
        pose.rz = self.rz + o.rz
        return pose

    def __sub__(self, o):
        pose = SA_MC_Pose()
        pose.x = self.x - o.x
        pose.y = self.y - o.y
        pose.z = self.z - o.z
        pose.rx = self.rx - o.rx
        pose.ry = self.ry - o.ry
        pose.rz = self.rz - o.rz
        return pose
    
    def __str__(self):
        return "5DOF Pose. x: %f, y: %f, z: %f, rx: %f, ry: %f, rz: %f" % \
            (self.x, self.y, self.z, self.rx, self.ry, self.rz)


def SA_MC_pose_to_dict(pose):
    """
    Convert a SA_MC Pose (C structure) to a coordinate dictionary (str) -> (double)
        pose: a Pose (C struct)
    returns: Coordinate dictionary (str) -> (double) of axis name to value
    """
    pos = {}
    pos['x'] = pose.x
    pos['y'] = pose.y
    pos['z'] = pose.z

    # Note: internally, the system uses metres and degrees for rotation
    pos['rx'] = math.radians(pose.rx)
    pos['rz'] = math.radians(pose.rz)
    return pos


def dict_to_SA_MC_pose(pos, base=SA_MC_Pose()):
    """
    Convert a coordinate dictionary (str) -> (double) to a SA_MC Pose C struct
        pos: Coordinate dictionary (str) -> (double) of axis name to value
        base: A SA_MC_Pose to use as a base. Otherwise, if axes are missing in the dict,
            the values will be initialized to the default values of 0.
    returns: a Pose (C struct)
    raises ValueError if an unsupported axis name is input
    """

    base.ry = 0

    # Note: internally, the system uses metres and degrees for rotation
    for an, v in pos.items():
        if an == "x":
            base.x = v
        elif an == "y":
            base.y = v
        elif an == "z":
            base.z = v
        elif an == "rx":
            base.rx = math.degrees(v)
        elif an == "rz":
            base.rz = math.degrees(v)
        else:
            raise ValueError("Invalid axis")
    return base


class MC_5DOF_DLL(CDLL):
    """
    Subclass of CDLL specific to SA_MC library, which handles error codes for
    all the functions automatically.
    """

    hwModel = 22000  # specifies the SA_MC 110.45 S (nano)

    # SmarAct MC error codes

    # No error
    SA_MC_OK = 0x0000
    # Unspecified error
    SA_MC_ERROR_OTHER = 0x0001
    # Invalid parameter in function call
    SA_MC_ERROR_INVALID_PARAMETER = 0x0002
    # Invalid locator in call to Open function
    SA_MC_ERROR_INVALID_LOCATOR = 0x0003
    # Undefined or inaccessible property in function call
    SA_MC_ERROR_INVALID_PROPERTY = 0x0004
    # Invalid handle in call to function
    SA_MC_ERROR_INVALID_HANDLE = 0x0005
    # Tried to use an unspported feature
    SA_MC_ERROR_NOT_SUPPORTED = 0x0006
    # Reached limit of simultaneously controllable devices
    SA_MC_ERROR_DEVICE_LIMIT_REACHED = 0x0007
    # Supplied buffer too small
    SA_MC_ERROR_QUERYBUFFER_SIZE = 0x0008
    # An operation has been canceled while waiting for a result
    SA_MC_ERROR_CANCELED = 0x0100
    # An operation has timed out
    SA_MC_ERROR_TIMEOUT = 0x0101
    # The pose specified in the Move command is invalid/unreachable
    SA_MC_ERROR_POSE_UNREACHABLE = 0x0200
    # Device has not been referenced
    SA_MC_ERROR_NOT_REFERENCED = 0x0201
    # An operation could not be started because the device is busy
    SA_MC_ERROR_BUSY = 0x0203
    # Positioners were blocked during movement
    SA_MC_ERROR_ENDSTOP_REACHED = 0x0300
    # The following error limit has been exceeded during movement
    SA_MC_ERROR_FOLLOWING_ERROR_LIMIT_REACHED = 0x0301
    # Positioner referencing failed
    SA_MC_ERROR_REFERENCING_FAILED = 0x0320
    # Could not load required hardware driver
    SA_MC_ERROR_DRIVER_FAILED = 0x0500
    # Could not find/connect to controller
    SA_MC_ERROR_CONNECT_FAILED = 0x0501
    # The device is not connected
    SA_MC_ERROR_NOT_CONNECTED = 0x0502
    # The controller doesn't provide the require features or configuration
    SA_MC_ERROR_CONTROLLER_CONFIGURATION = 0x0503
    # Error when communicating with controller
    SA_MC_ERROR_COMMUNICATION_FAILED = 0x0504

    # property symbols
    SA_MC_PKEY_PIVOT_POINT_MODE = 0x00001001
    SA_MC_PKEY_IS_REFERENCED = 0x00002a00
    SA_MC_PKEY_HOLD_TIME = 0x00002000
    SA_MC_PKEY_MAX_SPEED_LINEAR_AXES = 0x00002010
    SA_MC_PKEY_MAX_SPEED_ROTARY_AXES = 0x00002011
    SA_MC_PKEY_PIEZO_MAX_CLF_LINEAR_AXES = 0x00002020
    SA_MC_PKEY_PIEZO_MAX_CLF_ROTARY_AXES = 0x00002021
    SA_MC_PIVOT_POINT_MODE_RELATIVE = 0
    SA_MC_PIVOT_POINT_MODE_ABSOLUTE = 1

    # events
    SA_MC_EVENT_MOVEMENT_FINISHED = 0x0001

    # handles
    # handle value that means no object
    SA_MC_INVALID_HANDLE = 0xffffffff
    SA_MC_INFINITE = -1

    err_code = {
0x0000: "No error",
0x0001: "Unspecified error",
0x0002: "Invalid parameter in function call ",
0x0003: "Invalid locator in call to Open function ",
0x0004: "Undefined or inaccessible property in function call ",
0x0005: "Invalid handle in call to function ",
0x0006: "Tried to use an unspported feature",
0x0007: "Reached limit of simultaneously controllable devices ",
0x0008: "Supplied buffer too small",
0x0100: "An operation has been canceled while waiting for a result ",
0x0101: "An operation has timed out ",
0x0200: "The pose specified in the Move command is invalid/unreachable ",
0x0201: "Device has not been referenced ",
0x0203: "An operation could not be started because the device is busy ",
0x0300: "Positioners were blocked during movement ",
0x0301: "The following error limit has been exceeded during movement ",
0x0320: "Positioner referencing failed ",
0x0500: "Could not load required hardware driver",
0x0501: "Could not find/connect to controller",
0x0502: "The device is not connected",
0x0503: "The controller doesn't provide the require features or configuration",
0x0504: "Error when communicating with controller",
        }

    def __init__(self):
        if os.name == "nt":
            raise NotImplemented("Windows not yet supported")
            # WinDLL.__init__(self, "libSA_MC.dll")  # TODO check it works
            # atmcd64d.dll on 64 bits
        else:
            # Global so that its sub-libraries can access it
            CDLL.__init__(self, "libsmaractmc.so", RTLD_GLOBAL)

    def __getitem__(self, name):
        try:
            func = super(MC_5DOF_DLL, self).__getitem__(name)
        except Exception:
            raise AttributeError("Failed to find %s" % (name,))
        func.__name__ = name
        func.errcheck = self.sp_errcheck
        return func

    @staticmethod
    def sp_errcheck(result, func, args):
        """
        Analyse the return value of a call and raise an exception in case of
        error.
        Follows the ctypes.errcheck callback convention
        """
        if result != MC_5DOF_DLL.SA_MC_OK:
            raise SA_MCError(result)

        return result


class SA_MCError(Exception):
    """
    SA_MC Exception
    """

    def __init__(self, error_code):
        self.errno = error_code
        super(SA_MCError, self).__init__("Error %d. %s" % (error_code, MC_5DOF_DLL.err_code.get(error_code, "")))


class MC_5DOF(model.Actuator):

    def __init__(self, name, role, locator, axes, ref_on_init=False, linear_speed=0.01,
                 rotary_speed=0.0174533, **kwargs):
        """
        A driver for a SmarAct SA_MC Actuator, custom build for Delmic.
        Has 5 degrees of freedom
        This driver uses a DLL provided by SmarAct which connects via
        USB or TCP/IP using a locator string.

        name: (str)
        role: (str)
        locator: (str) Use "fake" for a simulator.
            For a real device, MCS controllers with USB interface can be addressed with the
            following locator syntax:
                usb:id:<id>
            where <id> is the first part of a USB devices serial number which
            is printed on the MCS controller.
            If the controller has a TCP/IP connection, use:
                network:<ip>:<port>
        ref_on_init: (bool) determines if the controller should automatically reference
            on initialization
        linear_speed: (double) the default speed (in m/s) of the linear actuators
        rotary_speed: (double) the default speed (in rad/s) of the rotary actuators
        axes: dict str (axis name) -> dict (axis parameters)
            The following axes must all be present:
            x , y, z, rx, rz
            note: internally in the driver, ry exists, but has a range of (0,0), so it
            is not included here

            axis parameters: {
                range: [float, float], default is -1 -> 1
                unit: (str) default will be set to 'm'
            }
        """
        if not axes:
            raise ValueError("Needs at least 1 axis.")

        if locator != "fake":
            self.core = MC_5DOF_DLL()
        else:
            self.core = FakeMC_5DOF_DLL()

        # Not to be mistaken with axes which is a simple public view
        self._axis_map = {}  # axis name -> axis number used by controller
        axes_def = {}  # axis name -> Axis object
        self._locator = locator

        # Require the user to define all 5 axes: x, y, z, rx, rz
        if set(axes.keys()) != {'x', 'y', 'z', 'rx', 'rz'}:
            raise ValueError("Invalid axes definition. Axes should contain x, y, z, rx, rz")

        for axis_name, axis_par in axes.items():
            try:
                axis_range = axis_par['range']
            except KeyError:
                logging.info("Axis %s has no range. Assuming (-1, 1)", axis_name)
                axis_range = (-1, 1)

            try:
                axis_unit = axis_par['unit']
            except KeyError:
                # check if linear
                if axis_name in {'x', 'y', 'z'}:
                    logging.info("Axis %s has no unit. Assuming m", axis_name)
                    axis_unit = "m"
                # otherwise must be a rotary axis
                else:
                    logging.info("Axis %s has no unit. Assuming rad", axis_name)
                    axis_unit = "rad"

            ad = model.Axis(canAbs=True, unit=axis_unit, range=axis_range)
            axes_def[axis_name] = ad

        # Connect to the device
        self._id = c_uint32(MC_5DOF_DLL.SA_MC_INVALID_HANDLE)

        option_string = "model %d\n locator %s" % (MC_5DOF_DLL.hwModel, locator)
                        
        options = c_char_p(option_string.encode("ascii"))

        self.core.SA_MC_Open(byref(self._id), options)
        logging.debug("Successfully connected to SA_MC Controller ID %d", self._id.value)
        model.Actuator.__init__(self, name, role, axes=axes_def, **kwargs)

        # Add metadata
        # TODO: Fix getting software version with a supported function
        self._swVersion = "Unknown"  # self.GetSwVersion()

        self._metadata[model.MD_SW_VERSION] = self._swVersion
        logging.debug("Using SA_MC library version %s", self._swVersion)

        self.position = model.VigilantAttribute({}, readonly=True)
        self._metadata[model.MD_PIVOT_POS] = self.GetPivot()

        # will take care of executing axis move asynchronously
        self._executor = CancellableThreadPoolExecutor(1)  # one task at a time

        referenced = self._is_referenced()
        # define the referenced VA from the query
        axes_ref = {a: referenced for a, i in self.axes.items()}
        # VA dict str(axis) -> bool
        self.referenced = model.VigilantAttribute(axes_ref, readonly=True)
        # If ref_on_init, referenced immediately.

        if referenced:
            logging.debug("SA_MC is referenced")
        else:
            if ref_on_init:
                self.reference().result()
            else:
                logging.warning("SA_MC is not referenced. The device will not function until referencing occurs.")

        # Use a default actuator speed
        self.set_linear_speed(linear_speed)
        self.set_rotary_speed(math.degrees(rotary_speed))
        self._updatePosition()

    def terminate(self):
        # should be safe to close the device multiple times if terminate is called more than once.
        self.core.SA_MC_Close(self._id)
        super(MC_5DOF, self).terminate()

    def updateMetadata(self, md):
        super(MC_5DOF, self).updateMetadata(md)
        try:
            pivot = md[model.MD_PIVOT_POS]
        except KeyError:
            # there is no pivot position set
            return

        if not isinstance(pivot, dict) and set(pivot.keys()) == {"x", "y", "z"}:
            raise ValueError("Invalid metadata, should be a coordinate dictionary but got %s." % (pivot,))

        logging.debug("Updating pivot point to %s.", pivot)
        self.SetPivot(pivot)

    """
    API Calls
    Functions to set the property values in the controller, categorized by data type
    """

    def SetProperty_f64(self, property_key, value):
        self.core.SA_MC_SetProperty_f64(self._id, c_uint32(property_key), c_double(value))

    def SetProperty_i32(self, property_key, value):
        self.core.SA_MC_SetProperty_i32(self._id, c_uint32(property_key), c_int32(value))

    def GetProperty_f64(self, property_key):
        ret_val = c_double()
        self.core.SA_MC_GetProperty_f64(self._id, c_uint32(property_key), byref(ret_val))
        return ret_val.value

    def GetProperty_i32(self, property_key):
        ret_val = c_int32()
        self.core.SA_MC_GetProperty_i32(self._id, c_uint32(property_key), byref(ret_val))
        return ret_val.value

    def WaitForEvent(self, timeout):
        # blocks until event is triggered or timeout.
        # returns the event code that was triggered
        ev = SA_MC_Event()
        self.core.SA_MC_WaitForEvent(self._id, byref(ev), c_int(int(timeout)))
        return ev

    def Reference(self):
        # Reference the controller. Note - this is asynchronous
        self.core.SA_MC_Reference(self._id)

    def _is_referenced(self):
        """
        Ask the controller if it is referenced
        """
        return bool(self.GetProperty_i32(MC_5DOF_DLL.SA_MC_PKEY_IS_REFERENCED))

    def Move(self, pos, hold_time=0, block=False):
        """
        Move to pose command.
        pos: (dict str -> float) axis name -> position
            This is converted to the pose C-struct which is sent to the SA_MC DLL
        hold_time: (float) specify in seconds how long to hold after the move.
            If set to float(inf), will hold forever until a stop command is issued.
        block: (bool) Set to True if the function should block until the move completes

        Raises: SA_MCError if a problem occurs
        """
        # convert into a smartpad pose
        newPose = dict_to_SA_MC_pose(pos, self.GetPose())

        if hold_time == float("inf"):
            ht = MC_5DOF_DLL.SA_MC_INFINITE
        else:
            ht = c_uint(int(hold_time * 1000.0))

        self.core.SA_MC_Move(self._id, byref(newPose), ht, c_int(block))

    def GetPose(self):
        """
        Get the current pose of the SA_MC

        returns: (dict str -> float): axis name -> position
        """
        pose = SA_MC_Pose()
        self.core.SA_MC_GetPose(self._id, byref(pose))
        return pose

    def Stop(self):
        """
        Stop command sent to the SA_MC
        """
        logging.debug("Stopping...")
        self.core.SA_MC_Stop(self._id)

    def set_linear_speed(self, value):
        """
        Set the linear speed of the SA_MC motion on all axes
        value: (double) indicating speed for all axes in m/s
        """
        logging.debug("Setting linear speed to %f", value)
        self.SetProperty_f64(MC_5DOF_DLL.SA_MC_PKEY_MAX_SPEED_LINEAR_AXES, value)

    def set_rotary_speed(self, value):
        """
        Set the rotary speed of the SA_MC motion for all axes
        value: (double) indicating speed for all axes in deg/s
        """
        logging.debug("Setting rotary speed to %f", value)
        self.SetProperty_f64(MC_5DOF_DLL.SA_MC_PKEY_MAX_SPEED_ROTARY_AXES, value)

    def get_linear_speed(self):
        """
        Returns (double) the linear speed of the SA_MC motion in m/s
        """
        return self.GetProperty_f64(MC_5DOF_DLL.SA_MC_PKEY_MAX_SPEED_LINEAR_AXES)

    def get_rotary_speed(self):
        """
        Returns (double) the rotary speed of the SA_MC motion in deg/s
        """
        return self.GetProperty_f64(MC_5DOF_DLL.SA_MC_PKEY_MAX_SPEED_ROTARY_AXES)

    def SetPivot(self, piv_dict):
        """
        Set the pivot point of the device

        piv_dict (dict str -> float): Position dictionary
            must have 'x', 'y', 'z'

        """
        pivot = SA_MC_Vec3()
        pivot.x = piv_dict["x"]
        pivot.y = piv_dict["y"]
        pivot.z = piv_dict["z"]
        self.core.SA_MC_SetPivot(self._id, byref(pivot))

    def GetPivot(self):
        """
        Get the pivot point from the controller

        returns: a dictionary (str -> float) of the axis and the pivot point

        """
        pivot = SA_MC_Vec3()
        self.core.SA_MC_GetPivot(self._id, byref(pivot))
        return {'x': pivot.x, 'y': pivot.y, 'z': pivot.z}

    def stop(self, axes=None):
        """
        Stop the SA_MC controller and update position
        """
        self.Stop()
        self._updatePosition()

    def _updatePosition(self):
        """
        update the position VA
        """
        try:
            p = SA_MC_pose_to_dict(self.GetPose())
        except SA_MCError as ex:
            if ex.errno == MC_5DOF_DLL.SA_MC_NOT_REFERENCED_ERROR:
                logging.warning("Position unknown because SA_MC is not referenced")
                p = {'x': 0, 'y': 0, 'z': 0, 'rx': 0, 'rz': 0}
            else:
                raise

        p = self._applyInversion(p)
        logging.debug("Updated position to %s", p)
        self.position._set_value(p, force_write=True)

    def _createMoveFuture(self, ref=False):
        """
        ref: if true, will use a different canceller
        Return (CancellableFuture): a future that can be used to manage a move
        """
        f = CancellableFuture()
        f._moving_lock = threading.Lock()  # taken while moving
        f._must_stop = threading.Event()  # cancel of the current future requested
        f._was_stopped = False  # if cancel was successful
        if not ref:
            f.task_canceller = self._cancelCurrentMove
        else:
            f.task_canceller = self._cancelReference
        return f

    @isasync
    def reference(self, _=None):
        """
        reference usually takes axes as an argument. However, the SA_MC references all
        axes together so this argument is extraneous.
        """
        f = self._createMoveFuture(ref=True)
        self._executor.submitf(f, self._doReference, f)
        return f

    def _doReference(self, future):
        """
        Actually runs the referencing code
        future (Future): the future it handles
        raise:
            IOError: if referencing failed due to hardware
            CancelledError if was cancelled
        """
        # Reset reference so that if it fails, it states the axes are not
        # referenced (anymore)
        with future._moving_lock:
            try:
                # set the referencing for all axes to fals
                self.referenced._value = {a: False for a in self.axes.keys()}

                # The SA_MC references all axes at once. This function blocks
                self.Reference()
                # wait till reference completes
                while True:
                    ev = self.WaitForEvent(MC_5DOF_DLL.SA_MC_INFINITE)
                    # check if move is done
                    if ev.type == MC_5DOF_DLL.SA_MC_EVENT_MOVEMENT_FINISHED:
                        break
                    else:
                        logging.warning("Returned event type 0x%x", ev.type)
                        # keep waiting as the referencing continues

                if self._is_referenced():
                    self.referenced._value = {a: True for a in self.axes.keys()}
                    self._updatePosition()
                    logging.info("Referencing successful.")

            except SA_MCError as ex:
                future._was_stopped = True
                # This occurs if a stop command interrupts referencing
                if ex.errno == MC_5DOF_DLL.SA_MC_ERROR_CANCELED:
                    logging.info("Referencing stopped: %s", ex)
                    raise CancelledError()
                else:
                    raise

            except Exception:
                logging.exception("Referencing failure")
                raise

            finally:
                # We only notify after updating the position so that when a listener
                # receives updates both values are already updated.
                # read-only so manually notify
                self.referenced.notify(self.referenced.value)

    @isasync
    def moveAbs(self, pos):
        """
        API call to absolute move
        """
        if not pos:
            return model.InstantaneousFuture()

        self._checkMoveAbs(pos)

        f = self._createMoveFuture()
        f = self._executor.submitf(f, self._doMoveAbs, f, pos)
        return f

    def _doMoveAbs(self, future, pos):
        """
        Blocking and cancellable absolute move
        future (Future): the future it handles
        _pos (dict str -> float): axis name -> absolute target position
        raise:
            SA_MCError: if the controller reported an error
            CancelledError: if cancelled before the end of the move
        """
        last_upd = time.time()
        dur = 30  # TODO: Calculate an estimated move duration
        end = time.time() + dur
        max_dur = dur * 2 + 1
        logging.debug("Expecting a move of %g s, will wait up to %g s", dur, max_dur)
        timeout = last_upd + max_dur

        with future._moving_lock:
            try:
                self.Move(pos, hold_time=float("inf"))
                while not future._must_stop.is_set():
                    ev = self.WaitForEvent(timeout)
                    # check if move is done
                    if ev.type == MC_5DOF_DLL.SA_MC_EVENT_MOVEMENT_FINISHED:
                        break

                    now = time.time()
                    if now > timeout:
                        logging.warning("Stopping move due to timeout after %g s.", max_dur)
                        self.stop()
                        raise TimeoutError("Move is not over after %g s, while "
                                           "expected it takes only %g s" %
                                           (max_dur, dur))

                    # Update the position from time to time (10 Hz)
                    if now - last_upd > 0.1:
                        self._updatePosition()
                        last_upd = time.time()

                    # Wait half of the time left (maximum 0.1 s)
                    left = end - time.time()
                    sleept = max(0.001, min(left / 2, 0.1))
                    future._must_stop.wait(sleept)
                else:
                    self.stop()
                    future._was_stopped = True
                    raise CancelledError()

            except SA_MCError as ex:
                future._was_stopped = True
                # This occurs if a stop command interrupts referencing
                if ex.errno == MC_5DOF_DLL.SA_MC_ERROR_CANCELED:
                    logging.debug("movement stopped: %s", ex)
                    raise CancelledError()
                else:
                    raise
            except Exception:
                logging.exception("Move failure")
                raise

            finally:
                self._updatePosition()

        logging.debug("move successfully completed")

    def _cancelCurrentMove(self, future):
        """
        Cancels the current move (both absolute or relative). Non-blocking.
        future (Future): the future to stop. Unused, only one future must be
         running at a time.
        return (bool): True if it successfully cancelled (stopped) the move.
        """
        # The difficulty is to synchronise correctly when:
        #  * the task is just starting (not finished requesting axes to move)
        #  * the task is finishing (about to say that it finished successfully)
        logging.debug("Canceling current move")

        future._must_stop.set()  # tell the thread taking care of the move it's over
        with future._moving_lock:
            if not future._was_stopped:
                logging.debug("Canceling failed")
            self._updatePosition()
            return future._was_stopped

    def _cancelReference(self, future):
        # The difficulty is to synchronize correctly when:
        #  * the task is just starting (about to request axes to move)
        #  * the task is finishing (about to say that it finished successfully)
        logging.debug("Canceling current referencing")

        self.Stop()
        future._must_stop.set()  # tell the thread taking care of the referencing it's over

        # Synchronise with the ending of the future
        with future._moving_lock:

            if not future._was_stopped:
                logging.debug("Cancelling failed")
            return future._was_stopped

    @isasync
    def moveRel(self, shift):
        """
        API call for relative move
        """
        if not shift:
            return model.InstantaneousFuture()

        self._checkMoveRel(shift)

        f = self._createMoveFuture()
        f = self._executor.submitf(f, self._doMoveRel, f, shift)
        return f

    def _doMoveRel(self, future, shift):
        """
        Do a relative move by converting it into an absolute move
        """
        pos = add_coord(self.position.value, shift)
        self._doMoveAbs(future, pos)


class FakeMC_5DOF_DLL(object):
    """
    Fake TrGlide DLL for simulator
    """

    def __init__(self):
        self.pose = SA_MC_Pose()
        self.target = SA_MC_Pose()
        self.properties = {
            MC_5DOF_DLL.SA_MC_PKEY_MAX_SPEED_LINEAR_AXES: c_double(0.1),
            MC_5DOF_DLL.SA_MC_PKEY_MAX_SPEED_ROTARY_AXES: c_double(5),
            MC_5DOF_DLL.SA_MC_PKEY_IS_REFERENCED: c_int32(0),
            }
        self._pivot = SA_MC_Vec3()

        # Specify ranges
        self._range = {}
        self._range['x'] = (-1, 1)
        self._range['y'] = (-1, 1)
        self._range['z'] = (-1, 1)
        self._range['rx'] = (-45, 45)
        self._range['ry'] = (0, 0)
        self._range['rz'] = (-45, 45)

        self.stopping = threading.Event()

        self._referencing = False

        self._current_move_start = time.time()
        self._current_move_finish = time.time()

    def _pose_in_range(self, pose):
        if self._range['x'][0] <= pose.x <= self._range['x'][1] and \
            self._range['y'][0] <= pose.y <= self._range['y'][1] and \
            self._range['z'][0] <= pose.z <= self._range['z'][1] and \
            self._range['rx'][0] <= pose.rx <= self._range['rx'][1] and \
            self._range['ry'][0] <= pose.ry <= self._range['ry'][1] and \
            self._range['rz'][0] <= pose.rz <= self._range['rz'][1]:
            return True
        else:
            return False

    """
    DLL functions (fake)
    These functions are provided by the real SA_MC DLL
    """

    def SA_MC_Open(self, id, options):
        logging.debug("sim MC5DOF: Starting")

    def SA_MC_Close(self, id):
        logging.debug("sim MC5DOF: Closing")

    def SA_MC_GetPivot(self, id, p_piv):
        val = _deref(p_piv, SA_MC_Vec3)
        val.value = self._pivot
        logging.debug("sim MC5DOF: Get pivot: (%f, %f, %f)" % (self._pivot.x, self._pivot.y, self._pivot.z))

    def SA_MC_SetPivot(self, id, p_piv):
        self._pivot = _deref(p_piv, SA_MC_Vec3)
        logging.debug("sim MC5DOF: Setting pivot to (%f, %f, %f)" % (self._pivot.x, self._pivot.y, self._pivot.z))

    def SA_MC_Move(self, id, p_pose, hold_time, block):
        self.stopping.clear()
        pose = _deref(p_pose, SA_MC_Pose)
        if self._pose_in_range(pose):
            self._current_move_finish = time.time() + 1.0
            self.target.x = pose.x
            self.target.y = pose.y
            self.target.z = pose.z
            self.target.rx = pose.rx
            self.target.ry = pose.ry
            self.target.rz = pose.rz

            logging.debug("sim MC5DOF: moving to target: %s" % (self.target,))
        else:
            raise SA_MCError(MC_5DOF_DLL.SA_MC_ERROR_POSE_UNREACHABLE)

    def SA_MC_GetPose(self, id, p_pose):
        pose = _deref(p_pose, SA_MC_Pose)
        pose.x = self.pose.x
        pose.y = self.pose.y
        pose.z = self.pose.z
        pose.rx = self.pose.rx
        pose.ry = self.pose.ry
        pose.rz = self.pose.rz
        logging.debug("sim MC5DOF: position: %s" % (self.pose,))
        return MC_5DOF_DLL.SA_MC_OK

    def SA_MC_Stop(self, id):
        logging.debug("sim MC5DOF: Stopping")
        self.stopping.set()

    def SA_MC_Reference(self, id):
        logging.debug("sim MC5DOF: Starting referencing...")
        self.properties[MC_5DOF_DLL.SA_MC_PKEY_IS_REFERENCED] = c_int32(0)
        self.stopping.clear()
        self._current_move_finish = time.time() + 1.0
        self._referencing = True

    def SA_MC_SetProperty_f64(self, id, prop, val):
        if not prop.value in self.properties:
            raise SA_MCError(MC_5DOF_DLL.SA_MC_ERROR_INVALID_PROPERTY)

        self.properties[prop.value] = val

    def SA_MC_SetProperty_i32(self, id, prop, val):
        if not prop.value in self.properties:
            raise SA_MCError(MC_5DOF_DLL.SA_MC_ERROR_INVALID_PROPERTY)

        self.properties[prop.value] = val

    def SA_MC_GetProperty_f64(self, id, prop, p_val):
        if not prop.value in self.properties:
            raise SA_MCError(MC_5DOF_DLL.SA_MC_ERROR_INVALID_PROPERTY)

        val = _deref(p_val, c_double)
        val.value = self.properties[prop.value].value

    def SA_MC_GetProperty_i32(self, id, prop, p_val):
        if not prop.value in self.properties:
            raise SA_MCError(MC_5DOF_DLL.SA_MC_ERROR_INVALID_PROPERTY)

        val = _deref(p_val, c_int32)
        val.value = self.properties[prop.value].value

    def SA_MC_WaitForEvent(self, id, p_ev, timeout):
        ev = _deref(p_ev, SA_MC_Event)
        time.sleep(1.0)
        ev.type = MC_5DOF_DLL.SA_MC_EVENT_MOVEMENT_FINISHED
        logging.debug("sim MC5DOF: movement complete")
        # if a reference move was in process...
        if self._referencing and not self.stopping.is_set():
            self.properties[MC_5DOF_DLL.SA_MC_PKEY_IS_REFERENCED] = c_int32(1)
            self._referencing = False  # finished referencing
            logging.debug("sim MC5DOF: Referencing complete")

# Classes associated with the SmarAct MCS2 Controller (standard)


class SA_CTL_TransmitHandle_t(c_uint32):
    pass


class SA_CTLDLL(CDLL):
    """
    Subclass of CDLL specific to SA_CTL library, which handles error codes for
    all the functions automatically.
    """

    hwModel = 0  # specifies the SA_CTL 110.45 S (nano)

    # SmarAct MCS2 error codes
    SA_CTL_ERROR_NONE = 0x0000
    SA_CTL_ERROR_UNKNOWN_COMMAND = 0x0001
    SA_CTL_ERROR_INVALID_PACKET_SIZE = 0x0002
    SA_CTL_ERROR_TIMEOUT = 0x0004
    SA_CTL_ERROR_INVALID_PROTOCOL = 0x0005
    SA_CTL_ERROR_BUFFER_UNDERFLOW = 0x000c
    SA_CTL_ERROR_BUFFER_OVERFLOW = 0x000d
    SA_CTL_ERROR_INVALID_FRAME_SIZE = 0x000e
    SA_CTL_ERROR_INVALID_PACKET = 0x0010
    SA_CTL_ERROR_INVALID_KEY = 0x0012
    SA_CTL_ERROR_INVALID_PARAMETER = 0x0013
    SA_CTL_ERROR_INVALID_DATA_TYPE = 0x0016
    SA_CTL_ERROR_INVALID_DATA = 0x0017
    SA_CTL_ERROR_HANDLE_LIMIT_REACHED = 0x0018
    SA_CTL_ERROR_ABORTED = 0x0019

    SA_CTL_ERROR_INVALID_DEVICE_INDEX = 0x0020
    SA_CTL_ERROR_INVALID_MODULE_INDEX = 0x0021
    SA_CTL_ERROR_INVALID_CHANNEL_INDEX = 0x0022

    SA_CTL_ERROR_PERMISSION_DENIED = 0x0023
    SA_CTL_ERROR_COMMAND_NOT_GROUPABLE = 0x0024
    SA_CTL_ERROR_MOVEMENT_LOCKED = 0x0025
    SA_CTL_ERROR_SYNC_FAILED = 0x0026
    SA_CTL_ERROR_INVALID_ARRAY_SIZE = 0x0027
    SA_CTL_ERROR_OVERRANGE = 0x0028
    SA_CTL_ERROR_INVALID_CONFIGURATION = 0x0029

    SA_CTL_ERROR_NO_HM_PRESENT = 0x0100
    SA_CTL_ERROR_NO_IOM_PRESENT = 0x0101
    SA_CTL_ERROR_NO_SM_PRESENT = 0x0102
    SA_CTL_ERROR_NO_SENSOR_PRESENT = 0x0103
    SA_CTL_ERROR_SENSOR_DISABLED = 0x0104
    SA_CTL_ERROR_POWER_SUPPLY_DISABLED = 0x0105
    SA_CTL_ERROR_AMPLIFIER_DISABLED = 0x0106
    SA_CTL_ERROR_INVALID_SENSOR_MODE = 0x0107
    SA_CTL_ERROR_INVALID_ACTUATOR_MODE = 0x0108
    SA_CTL_ERROR_INVALID_INPUT_TRIG_MODE = 0x0109
    SA_CTL_ERROR_INVALID_CONTROL_OPTIONS = 0x010a
    SA_CTL_ERROR_INVALID_REFERENCE_TYPE = 0x010b
    SA_CTL_ERROR_INVALID_ADJUSTMENT_STATE = 0x010c
    SA_CTL_ERROR_INVALID_INFO_TYPE = 0x010d
    SA_CTL_ERROR_NO_FULL_ACCESS = 0x010e
    SA_CTL_ERROR_ADJUSTMENT_FAILED = 0x010f
    SA_CTL_ERROR_MOVEMENT_OVERRIDDEN = 0x0110
    SA_CTL_ERROR_NOT_CALIBRATED = 0x0111
    SA_CTL_ERROR_NOT_REFERENCED = 0x0112
    SA_CTL_ERROR_NOT_ADJUSTED = 0x0113
    SA_CTL_ERROR_SENSOR_TYPE_NOT_SUPPORTED = 0x0114
    SA_CTL_ERROR_CONTROL_LOOP_INPUT_DISABLED = 0x0115
    SA_CTL_ERROR_INVALID_CONTROL_LOOP_INPUT = 0x0116
    SA_CTL_ERROR_UNEXPECTED_SENSOR_DATA = 0x0117
    SA_CTL_ERROR_NOT_PHASED = 0x0118
    SA_CTL_ERROR_POSITIONER_FAULT = 0x0119
    SA_CTL_ERROR_DRIVER_FAULT = 0x011a
    SA_CTL_ERROR_POSITIONER_TYPE_NOT_SUPPORTED = 0x011b
    SA_CTL_ERROR_POSITIONER_TYPE_NOT_IDENTIFIED = 0x011c
    SA_CTL_ERROR_POSITIONER_TYPE_NOT_WRITEABLE = 0x011e
    SA_CTL_ERROR_INVALID_ACTUATOR_TYPE = 0x0121

    SA_CTL_ERROR_BUSY_MOVING = 0x0150
    SA_CTL_ERROR_BUSY_CALIBRATING = 0x0151
    SA_CTL_ERROR_BUSY_REFERENCING = 0x0152
    SA_CTL_ERROR_BUSY_ADJUSTING = 0x0153

    SA_CTL_ERROR_END_STOP_REACHED = 0x0200
    SA_CTL_ERROR_FOLLOWING_ERR_LIMIT = 0x0201
    SA_CTL_ERROR_RANGE_LIMIT_REACHED = 0x0202
    SA_CTL_ERROR_POSITIONER_OVERLOAD = 0x0203
    SA_CTL_ERROR_POWER_SUPPLY_FAILURE = 0x0205
    SA_CTL_ERROR_OVER_TEMPERATURE = 0x0206
    SA_CTL_ERROR_POWER_SUPPLY_OVERLOAD = 0x0208

    SA_CTL_ERROR_INVALID_STREAM_HANDLE = 0x0300
    SA_CTL_ERROR_INVALID_STREAM_CONFIGURATION = 0x0301
    SA_CTL_ERROR_INSUFFICIENT_FRAMES = 0x0302
    SA_CTL_ERROR_BUSY_STREAMING = 0x0303

    SA_CTL_ERROR_HM_INVALID_SLOT_INDEX = 0x0400
    SA_CTL_ERROR_HM_INVALID_CHANNEL_INDEX = 0x0401
    SA_CTL_ERROR_HM_INVALID_GROUP_INDEX = 0x0402
    SA_CTL_ERROR_HM_INVALID_CH_GRP_INDEX = 0x0403

    SA_CTL_ERROR_INTERNAL_COMMUNICATION = 0x0500

    SA_CTL_ERROR_FEATURE_NOT_SUPPORTED = 0x7ffd
    SA_CTL_ERROR_FEATURE_NOT_IMPLEMENTED = 0x7ffe

    err_code = {
        0x0000: "NONE",
        0x0001: "UNKNOWN_COMMAND",
        0x0002: "INVALID_PACKET_SIZE",
        0x0004: "TIMEOUT",
        0x0005: "INVALID_PROTOCOL",
        0x000c: "BUFFER_UNDERFLOW",
        0x000d: "BUFFER_OVERFLOW",
        0x000e: "INVALID_FRAME_SIZE",
        0x0010: "INVALID_PACKET",
        0x0012: "INVALID_KEY",
        0x0013: "INVALID_PARAMETER",
        0x0016: "INVALID_DATA_TYPE",
        0x0017: "INVALID_DATA",
        0x0018: "HANDLE_LIMIT_REACHED",
        0x0019: "ABORTED",
        0x0020: "INVALID_DEVICE_INDEX",
        0x0021: "INVALID_MODULE_INDEX",
        0x0022: "INVALID_CHANNEL_INDEX",
        0x0023: "PERMISSION_DENIED",
        0x0024: "COMMAND_NOT_GROUPABLE",
        0x0025: "MOVEMENT_LOCKED",
        0x0026: "SYNC_FAILED",
        0x0027: "INVALID_ARRAY_SIZE",
        0x0028: "OVERRANGE",
        0x0029: "INVALID_CONFIGURATION",
        0x0100: "NO_HM_PRESENT",
        0x0101: "NO_IOM_PRESENT",
        0x0102: "NO_SM_PRESENT",
        0x0103: "NO_SENSOR_PRESENT",
        0x0104: "SENSOR_DISABLED",
        0x0105: "POWER_SUPPLY_DISABLED",
        0x0106: "AMPLIFIER_DISABLED",
        0x0107: "INVALID_SENSOR_MODE",
        0x0108: "INVALID_ACTUATOR_MODE",
        0x0109: "INVALID_INPUT_TRIG_MODE",
        0x010a: "INVALID_CONTROL_OPTIONS",
        0x010b: "INVALID_REFERENCE_TYPE",
        0x010c: "INVALID_ADJUSTMENT_STATE",
        0x010d: "INVALID_INFO_TYPE",
        0x010e: "NO_FULL_ACCESS",
        0x010f: "ADJUSTMENT_FAILED",
        0x0110: "MOVEMENT_OVERRIDDEN",
        0x0111: "NOT_CALIBRATED",
        0x0112: "NOT_REFERENCED",
        0x0113: "NOT_ADJUSTED",
        0x0114: "SENSOR_TYPE_NOT_SUPPORTED",
        0x0115: "CONTROL_LOOP_INPUT_DISABLED",
        0x0116: "INVALID_CONTROL_LOOP_INPUT",
        0x0117: "UNEXPECTED_SENSOR_DATA",
        0x0118: "NOT_PHASED",
        0x0119: "POSITIONER_FAULT",
        0x011a: "DRIVER_FAULT",
        0x011b: "POSITIONER_TYPE_NOT_SUPPORTED",
        0x011c: "POSITIONER_TYPE_NOT_IDENTIFIED",
        0x011e: "POSITIONER_TYPE_NOT_WRITEABLE",
        0x0121: "INVALID_ACTUATOR_TYPE",
        0x0150: "BUSY_MOVING",
        0x0151: "BUSY_CALIBRATING",
        0x0152: "BUSY_REFERENCING",
        0x0153: "BUSY_ADJUSTING",
        0x0200: "END_STOP_REACHED",
        0x0201: "FOLLOWING_ERR_LIMIT",
        0x0202: "RANGE_LIMIT_REACHED",
        0x0203: "POSITIONER_OVERLOAD",
        0x0205: "POWER_SUPPLY_FAILURE",
        0x0206: "OVER_TEMPERATURE",
        0x0208: "POWER_SUPPLY_OVERLOAD",
        0x0300: "INVALID_STREAM_HANDLE",
        0x0301: "INVALID_STREAM_CONFIGURATION",
        0x0302: "INSUFFICIENT_FRAMES",
        0x0303: "BUSY_STREAMING",
        0x0400: "HM_INVALID_SLOT_INDEX",
        0x0401: "HM_INVALID_CHANNEL_INDEX",
        0x0402: "HM_INVALID_GROUP_INDEX",
        0x0403: "HM_INVALID_CH_GRP_INDEX",
        0x0500: "INTERNAL_COMMUNICATION",
        0x7ffd: "FEATURE_NOT_SUPPORTED",
        0x7ffe: "FEATURE_NOT_IMPLEMENTED",
        0xf001: "INVALID LOCATOR STRING",
        0xf003: "NOT INITIALIZED",
        0xf004: "COMMUNICATION FAILED",
        0xf007: "INVALID DEVICE HANDLE",
        0xf008: "INVALID TRANSMIT HANDLE",
        0xf010: "CANCELLED",
        0xf013: "DRIVER FAILURE",
        0xf01a: "DEVICE_NOT_FOUND",
    }

    # device states
    SA_CTL_DEV_STATE_BIT_HM_PRESENT = 0x00000001
    SA_CTL_DEV_STATE_BIT_MOVEMENT_LOCKED = 0x00000002
    SA_CTL_DEV_STATE_BIT_INTERNAL_COMM_FAILURE = 0x00000100
    SA_CTL_DEV_STATE_BIT_IS_STREAMING = 0x00001000

    # module states
    SA_CTL_MOD_STATE_BIT_SM_PRESENT = 0x00000001
    SA_CTL_MOD_STATE_BIT_BOOSTER_PRESENT = 0x00000002
    SA_CTL_MOD_STATE_BIT_ADJUSTMENT_ACTIVE = 0x00000004
    SA_CTL_MOD_STATE_BIT_IOM_PRESENT = 0x00000008
    SA_CTL_MOD_STATE_BIT_INTERNAL_COMM_FAILURE = 0x00000100
    SA_CTL_MOD_STATE_BIT_FAN_FAILURE = 0x00000800
    SA_CTL_MOD_STATE_BIT_POWER_SUPPLY_FAILURE = 0x00001000
    SA_CTL_MOD_STATE_BIT_HIGH_VOLTAGE_FAILURE = 0x00001000  # deprecated
    SA_CTL_MOD_STATE_BIT_POWER_SUPPLY_OVERLOAD = 0x00002000
    SA_CTL_MOD_STATE_BIT_HIGH_VOLTAGE_OVERLOAD = 0x00002000  # deprecated
    SA_CTL_MOD_STATE_BIT_OVER_TEMPERATURE = 0x00004000

    # channel states
    SA_CTL_CH_STATE_BIT_ACTIVELY_MOVING = 0x00000001
    SA_CTL_CH_STATE_BIT_CLOSED_LOOP_ACTIVE = 0x00000002
    SA_CTL_CH_STATE_BIT_CALIBRATING = 0x00000004
    SA_CTL_CH_STATE_BIT_REFERENCING = 0x00000008
    SA_CTL_CH_STATE_BIT_MOVE_DELAYED = 0x00000010
    SA_CTL_CH_STATE_BIT_SENSOR_PRESENT = 0x00000020
    SA_CTL_CH_STATE_BIT_IS_CALIBRATED = 0x00000040
    SA_CTL_CH_STATE_BIT_IS_REFERENCED = 0x00000080
    SA_CTL_CH_STATE_BIT_END_STOP_REACHED = 0x00000100
    SA_CTL_CH_STATE_BIT_RANGE_LIMIT_REACHED = 0x00000200
    SA_CTL_CH_STATE_BIT_FOLLOWING_LIMIT_REACHED = 0x00000400
    SA_CTL_CH_STATE_BIT_MOVEMENT_FAILED = 0x00000800
    SA_CTL_CH_STATE_BIT_IS_STREAMING = 0x00001000
    SA_CTL_CH_STATE_BIT_POSITIONER_OVERLOAD = 0x00002000
    SA_CTL_CH_STATE_BIT_OVER_TEMPERATURE = 0x00004000
    SA_CTL_CH_STATE_BIT_REFERENCE_MARK = 0x00008000
    SA_CTL_CH_STATE_BIT_IS_PHASED = 0x00010000
    SA_CTL_CH_STATE_BIT_POSITIONER_FAULT = 0x00020000
    SA_CTL_CH_STATE_BIT_AMPLIFIER_ENABLED = 0x00040000

    # hand control module states
    SA_CTL_HM_STATE_BIT_INTERNAL_COMM_FAILURE = 0x0100
    SA_CTL_HM_STATE_BIT_IS_INTERNAL = 0x0200

    # property keys
    SA_CTL_PKEY_NUMBER_OF_CHANNELS = 0x020F0017
    SA_CTL_PKEY_NUMBER_OF_BUS_MODULES = 0x020F0016
    SA_CTL_PKEY_INTERFACE_TYPE = 0x020F0066
    SA_CTL_PKEY_DEVICE_STATE = 0x020F000F
    SA_CTL_PKEY_DEVICE_SERIAL_NUMBER = 0x020F005E
    SA_CTL_PKEY_DEVICE_NAME = 0x020F003D
    SA_CTL_PKEY_EMERGENCY_STOP_MODE = 0x020F0088
    SA_CTL_PKEY_NETWORK_DISCOVER_MODE = 0x020F0159
    SA_CTL_PKEY_NETWORK_DHCP_TIMEOUT = 0x020F015C
    # module
    SA_CTL_PKEY_POWER_SUPPLY_ENABLED = 0x02030010
    SA_CTL_PKEY_NUMBER_OF_BUS_MODULE_CHANNELS = 0x02030017
    SA_CTL_PKEY_MODULE_TYPE = 0x02030066
    SA_CTL_PKEY_MODULE_STATE = 0x0203000F
    # positioner
    SA_CTL_PKEY_STARTUP_OPTIONS = 0x0A02005D
    SA_CTL_PKEY_AMPLIFIER_ENABLED = 0x0302000D
    SA_CTL_PKEY_AMPLIFIER_MODE = 0x030200BF
    SA_CTL_PKEY_POSITIONER_CONTROL_OPTIONS = 0x0302005D
    SA_CTL_PKEY_ACTUATOR_MODE = 0x03020019
    SA_CTL_PKEY_CONTROL_LOOP_INPUT = 0x03020018
    SA_CTL_PKEY_SENSOR_INPUT_SELECT = 0x0302009D
    SA_CTL_PKEY_POSITIONER_TYPE = 0x0302003C
    SA_CTL_PKEY_POSITIONER_TYPE_NAME = 0x0302003D
    SA_CTL_PKEY_MOVE_MODE = 0x03050087
    SA_CTL_PKEY_CHANNEL_TYPE = 0x02020066
    SA_CTL_PKEY_CHANNEL_STATE = 0x0305000F
    SA_CTL_PKEY_POSITION = 0x0305001D
    SA_CTL_PKEY_TARGET_POSITION = 0x0305001E
    SA_CTL_PKEY_SCAN_POSITION = 0x0305001F
    SA_CTL_PKEY_SCAN_VELOCITY = 0x0305002A
    SA_CTL_PKEY_HOLD_TIME = 0x03050028
    SA_CTL_PKEY_MOVE_VELOCITY = 0x03050029
    SA_CTL_PKEY_MOVE_ACCELERATION = 0x0305002B
    SA_CTL_PKEY_MAX_CL_FREQUENCY = 0x0305002F
    SA_CTL_PKEY_DEFAULT_MAX_CL_FREQUENCY = 0x03050057
    SA_CTL_PKEY_STEP_FREQUENCY = 0x0305002E
    SA_CTL_PKEY_STEP_AMPLITUDE = 0x03050030
    SA_CTL_PKEY_FOLLOWING_ERROR_LIMIT = 0x03050055
    SA_CTL_PKEY_FOLLOWING_ERROR = 0x03020055
    SA_CTL_PKEY_BROADCAST_STOP_OPTIONS = 0x0305005D
    SA_CTL_PKEY_SENSOR_POWER_MODE = 0x03080019
    SA_CTL_PKEY_SENSOR_POWER_SAVE_DELAY = 0x03080054
    SA_CTL_PKEY_POSITION_MEAN_SHIFT = 0x03090022
    SA_CTL_PKEY_SAFE_DIRECTION = 0x03090027
    SA_CTL_PKEY_CL_INPUT_SENSOR_VALUE = 0x0302001D
    SA_CTL_PKEY_CL_INPUT_AUX_VALUE = 0x030200B2
    SA_CTL_PKEY_TARGET_TO_ZERO_VOLTAGE_HOLD_TH = 0x030200B9
    # scale
    SA_CTL_PKEY_LOGICAL_SCALE_OFFSET = 0x02040024
    SA_CTL_PKEY_LOGICAL_SCALE_INVERSION = 0x02040025
    SA_CTL_PKEY_RANGE_LIMIT_MIN = 0x02040020
    SA_CTL_PKEY_RANGE_LIMIT_MAX = 0x02040021
    SA_CTL_PKEY_DEFAULT_RANGE_LIMIT_MIN = 0x020400C0
    SA_CTL_PKEY_DEFAULT_RANGE_LIMIT_MAX = 0x020400C1
    # calibration
    SA_CTL_PKEY_CALIBRATION_OPTIONS = 0x0306005D
    SA_CTL_PKEY_SIGNAL_CORRECTION_OPTIONS = 0x0306001C
    # referencing
    SA_CTL_PKEY_REFERENCING_OPTIONS = 0x0307005D
    SA_CTL_PKEY_DIST_CODE_INVERTED = 0x0307000E
    SA_CTL_PKEY_DISTANCE_TO_REF_MARK = 0x030700A2
    # tuning and customizing
    SA_CTL_PKEY_POS_MOVEMENT_TYPE = 0x0309003F
    SA_CTL_PKEY_POS_IS_CUSTOM_TYPE = 0x03090041
    SA_CTL_PKEY_POS_BASE_UNIT = 0x03090042
    SA_CTL_PKEY_POS_BASE_RESOLUTION = 0x03090043
    SA_CTL_PKEY_POS_HEAD_TYPE = 0x0309008E
    SA_CTL_PKEY_POS_REF_TYPE = 0x03090048
    SA_CTL_PKEY_POS_P_GAIN = 0x0309004B
    SA_CTL_PKEY_POS_I_GAIN = 0x0309004C
    SA_CTL_PKEY_POS_D_GAIN = 0x0309004D
    SA_CTL_PKEY_POS_PID_SHIFT = 0x0309004E
    SA_CTL_PKEY_POS_ANTI_WINDUP = 0x0309004F
    SA_CTL_PKEY_POS_ESD_DIST_TH = 0x03090050
    SA_CTL_PKEY_POS_ESD_COUNTER_TH = 0x03090051
    SA_CTL_PKEY_POS_TARGET_REACHED_TH = 0x03090052
    SA_CTL_PKEY_POS_TARGET_HOLD_TH = 0x03090053
    SA_CTL_PKEY_POS_SAVE = 0x0309000A
    SA_CTL_PKEY_POS_WRITE_PROTECTION = 0x0309000D
    # streaming
    SA_CTL_PKEY_STREAM_BASE_RATE = 0x040F002C
    SA_CTL_PKEY_STREAM_EXT_SYNC_RATE = 0x040F002D
    SA_CTL_PKEY_STREAM_OPTIONS = 0x040F005D
    SA_CTL_PKEY_STREAM_LOAD_MAX = 0x040F0301
    # diagnostic
    SA_CTL_PKEY_CHANNEL_ERROR = 0x0502007A
    SA_CTL_PKEY_CHANNEL_TEMPERATURE = 0x05020034
    SA_CTL_PKEY_BUS_MODULE_TEMPERATURE = 0x05030034
    SA_CTL_PKEY_POSITIONER_FAULT_REASON = 0x05020113
    SA_CTL_PKEY_MOTOR_LOAD = 0x05020115
    # io module
    SA_CTL_PKEY_IO_MODULE_OPTIONS = 0x0603005D
    SA_CTL_PKEY_IO_MODULE_VOLTAGE = 0x06030031
    SA_CTL_PKEY_IO_MODULE_ANALOG_INPUT_RANGE = 0x060300A0
    # auxiliary
    SA_CTL_PKEY_AUX_POSITIONER_TYPE = 0x0802003C
    SA_CTL_PKEY_AUX_POSITIONER_TYPE_NAME = 0x0802003D
    SA_CTL_PKEY_AUX_INPUT_SELECT = 0x08020018
    SA_CTL_PKEY_AUX_IO_MODULE_INPUT_INDEX = 0x081100AA
    SA_CTL_PKEY_AUX_SENSOR_MODULE_INPUT_INDEX = 0x080B00AA
    SA_CTL_PKEY_AUX_IO_MODULE_INPUT0_VALUE = 0x08110000
    SA_CTL_PKEY_AUX_IO_MODULE_INPUT1_VALUE = 0x08110001
    SA_CTL_PKEY_AUX_SENSOR_MODULE_INPUT0_VALUE = 0x080B0000
    SA_CTL_PKEY_AUX_SENSOR_MODULE_INPUT1_VALUE = 0x080B0001
    SA_CTL_PKEY_AUX_DIRECTION_INVERSION = 0x0809000E
    SA_CTL_PKEY_AUX_DIGITAL_INPUT_VALUE = 0x080300AD
    SA_CTL_PKEY_AUX_DIGITAL_OUTPUT_VALUE = 0x080300AE
    SA_CTL_PKEY_AUX_DIGITAL_OUTPUT_SET = 0x080300B0
    SA_CTL_PKEY_AUX_DIGITAL_OUTPUT_CLEAR = 0x080300B1
    SA_CTL_PKEY_AUX_ANALOG_OUTPUT_VALUE0 = 0x08030000
    SA_CTL_PKEY_AUX_ANALOG_OUTPUT_VALUE1 = 0x08030001
    # threshold detector
    SA_CTL_PKEY_THD_INPUT_SELECT = 0x09020018
    SA_CTL_PKEY_THD_IO_MODULE_INPUT_INDEX = 0x091100AA
    SA_CTL_PKEY_THD_SENSOR_MODULE_INPUT_INDEX = 0x090B00AA
    SA_CTL_PKEY_THD_THRESHOLD_HIGH = 0x090200B4
    SA_CTL_PKEY_THD_THRESHOLD_LOW = 0x090200B5
    SA_CTL_PKEY_THD_INVERSION = 0x0902000E
    # input trigger
    SA_CTL_PKEY_DEV_INPUT_TRIG_MODE = 0x060D0087
    SA_CTL_PKEY_DEV_INPUT_TRIG_CONDITION = 0x060D005A
    # output trigger
    SA_CTL_PKEY_CH_OUTPUT_TRIG_MODE = 0x060E0087
    SA_CTL_PKEY_CH_OUTPUT_TRIG_POLARITY = 0x060E005B
    SA_CTL_PKEY_CH_OUTPUT_TRIG_PULSE_WIDTH = 0x060E005C
    SA_CTL_PKEY_CH_POS_COMP_START_THRESHOLD = 0x060E0058
    SA_CTL_PKEY_CH_POS_COMP_INCREMENT = 0x060E0059
    SA_CTL_PKEY_CH_POS_COMP_DIRECTION = 0x060E0026
    SA_CTL_PKEY_CH_POS_COMP_LIMIT_MIN = 0x060E0020
    SA_CTL_PKEY_CH_POS_COMP_LIMIT_MAX = 0x060E0021
    # hand control module
    SA_CTL_PKEY_HM_STATE = 0x020C000F
    SA_CTL_PKEY_HM_LOCK_OPTIONS = 0x020C0083
    SA_CTL_PKEY_HM_DEFAULT_LOCK_OPTIONS = 0x020C0084
    # api
    SA_CTL_PKEY_API_EVENT_NOTIFICATION_OPTIONS = 0xF010005D
    SA_CTL_PKEY_EVENT_NOTIFICATION_OPTIONS = 0xF010005D  # deprecated
    SA_CTL_PKEY_API_AUTO_RECONNECT = 0xF01000A1
    SA_CTL_PKEY_AUTO_RECONNECT = 0xF01000A1  # deprecated

    # move modes
    SA_CTL_MOVE_MODE_CL_ABSOLUTE = 0
    SA_CTL_MOVE_MODE_CL_RELATIVE = 1
    SA_CTL_MOVE_MODE_SCAN_ABSOLUTE = 2
    SA_CTL_MOVE_MODE_SCAN_RELATIVE = 3
    SA_CTL_MOVE_MODE_STEP = 4

    SA_CTL_INFINITE = 0xffffffff

    def __init__(self):
        if os.name == "nt":
            raise NotImplemented("Windows not yet supported")
            # WinDLL.__init__(self, "libSA_CTL.dll")  # TODO check it works
            # atmcd64d.dll on 64 bits
        else:
            # Global so that its sub-libraries can access it
            CDLL.__init__(self, "libsmaractctl.so", RTLD_GLOBAL)

    def __getitem__(self, name):
        try:
            func = super(SA_CTLDLL, self).__getitem__(name)
        except Exception:
            raise AttributeError("Failed to find %s" % (name,))
        func.__name__ = name
        func.errcheck = self.sp_errcheck
        return func

    @staticmethod
    def sp_errcheck(result, func, args):
        """
        Analyse the retuhwModelrn value of a call and raise an exception in case of
        error.
        Follows the ctypes.errcheck callback convention
        """
        if result != SA_CTLDLL.SA_CTL_ERROR_NONE:
            raise SA_CTLError(result)

        return result


class SA_CTLError(Exception):
    """
    SA_CTL Exception
    """

    def __init__(self, error_code):
        self.errno = error_code
        super(SA_CTLError, self).__init__("Error 0x%x. %s" % (error_code, SA_CTLDLL.err_code.get(error_code, "")))


class MCS2(model.Actuator):

    def __init__(self, name, role, locator, ref_on_init=False, axes=None, speed=1e-3, accel=1e-3,
                 hold_time=float('inf'), ** kwargs):
        """
        A driver for a SmarAct MCS2 Actuator.
        This driver uses a DLL provided by SmarAct which connects via
        USB or TCP/IP using a locator string.

        name: (str)
        role: (str)
        locator: (str) Use "fake" for a simulator.
            For a real device, MCS controllers with USB interface can be addressed with the
            following locator syntax:
                usb:id:<id>
            where <id> is the first part of a USB devices serial number which
            is printed on the MCS controller.
            If the controller has a TCP/IP connection, use:
                network:<ip>:<port>
        ref_on_init: (bool) determines if the controller should automatically reference
            on initialization
        hold_time (float): the hold time, in seconds, for the actuator after the target position is reached.
            Default is float('inf') or infinite. Can be also set to 0 to disable hold.
            Is set to the same value for all channels.
        axes: dict str (axis name) -> dict (axis parameters)
            axis parameters: {
                range: [float, float], default is -1 -> 1
                unit: (str) default will be set to 'm'
            }
        """
        if not axes:
            raise ValueError("Needs at least 1 axis.")

        if locator != "fake":
            self.core = SA_CTLDLL()
        else:
            self.core = FakeMCS2_DLL()

        # Not to be mistaken with axes which is a simple public view
        self._axis_map = {}  # axis name -> axis number used by controller
        axes_def = {}  # axis name -> Axis object
        self._locator = locator

        for axis_name, axis_par in axes.items():
            try:
                axis_range = axis_par['range']
            except KeyError:
                logging.info("Axis %s has no range. Assuming (-1, 1)", axis_name)
                axis_range = (-1, 1)

            try:
                axis_unit = axis_par['unit']
            except KeyError:
                logging.info("Axis %s has no unit. Assuming m", axis_name)
                axis_unit = "m"

            try:
                axis_channel = axis_par['channel']
            except KeyError:
                raise ValueError("Axis %s has no channel." % axis_name)

            ad = model.Axis(canAbs=True, unit=axis_unit, range=axis_range)
            axes_def[axis_name] = ad
            self._axis_map[axis_name] = axis_channel

        # Connect to the device
        self._id = c_uint32(0)

        logging.debug("Connecting to locator %s", locator)
        self.core.SA_CTL_Open(byref(self._id), c_char_p(locator.encode("ascii")), c_char_p(b""))
        logging.debug("Successfully connected to SA_CTL Controller ID %d with %d channels", self._id.value, self.get_number_of_channels())
        model.Actuator.__init__(self, name, role, axes=axes_def, **kwargs)

        # Add metadata
        self._swVersion = self.core.SA_CTL_GetFullVersionString()
        self._hwVersion = self.GetProperty_i32(SA_CTLDLL.SA_CTL_PKEY_DEVICE_NAME, 0)

        self._metadata[model.MD_SW_VERSION] = self._swVersion
        self._metadata[model.MD_HW_VERSION] = self._hwVersion
        logging.debug("Using SA_CTL library version %s", self._swVersion)

        self.position = model.VigilantAttribute({}, readonly=True)

        # will take care of executing axis move asynchronously
        self._executor = CancellableThreadPoolExecutor(1)  # one task at a time

        # define the referenced VA from the query
        axes_ref = {a: self._is_channel_referenced(i) for a, i in self._axis_map.items()}
        # VA dict str(axis) -> bool
        self.referenced = model.VigilantAttribute(axes_ref, readonly=True)
        # If ref_on_init, referenced immediately.

        if all(referenced == True for _, referenced in axes_ref.items()):
            logging.debug("SA_CTL is referenced")
        else:
            if ref_on_init:
                self.reference().result()
            else:
                logging.warning("SA_CTL is not referenced. The device will not function until referencing occurs.")

        self._updatePosition()

        for name, channel in self._axis_map.items():
            self._set_speed(channel, speed)
            self._set_accel(channel, accel)
            self._set_hold_time(channel, hold_time)

        self._speed = {}
        self._updateSpeed()

        self._accel = {}
        self._updateAccel()

    def terminate(self):
        # should be safe to close the device multiple times if terminate is called more than once.
        self.core.SA_CTL_Close(self._id)
        super(MCS2, self).terminate()

    @staticmethod
    def scan():
        """
        Util function to find all of the MCS2 controllers
        returns: set of tuples (name, dict) with dict str -> str
            the dict just has the locator string
        """
        core = SA_CTLDLL()
        b_len = 1024
        buf = create_string_buffer(b_len)
        core.SA_CTL_FindDevices(c_char_p(""), buf, byref(c_size_t(b_len)))
        locators = buf.value.encode('string_escape')

        devices = set()
        for counter, loc in enumerate(locators):
            devices.append(("MCS2 %d" % (counter,), {"locator": loc}))

        return devices

    # API Calls

    # Functions to set the property values in the controller, categorized by data type
    def SetProperty_f64(self, property_key, idx, value):
        """
        property_key (int32): property key symbol
        idx (int): channel
        value (double): value to set
        """
        self.core.SA_CTL_SetProperty_f64(self._id, c_int8(idx), c_uint32(property_key), c_double(value))

    def SetProperty_i32(self, property_key, idx, value):
        """
        property_key (int32): property key symbol
        idx (int): channel
        value (int32): value to set
        """
        self.core.SA_CTL_SetProperty_i32(self._id, c_int8(idx), c_uint32(property_key), c_int32(value))

    def SetProperty_i64(self, property_key, idx, value):
        """
        property_key (int64): property key symbol
        idx (int): channel
        value (int64): value to set
        """
        self.core.SA_CTL_SetProperty_i64(self._id, c_int8(idx), c_uint32(property_key), c_int64(value))

    def GetProperty_f64(self, property_key, idx):
        """
        property_key (int32): property key symbol
        idx (int): channel
        returns (float) the value
        """
        ret_val = c_double()
        self.core.SA_CTL_GetProperty_f64(self._id, c_int8(idx), c_uint32(property_key), byref(ret_val), c_size_t(0))
        return ret_val.value

    def GetProperty_i32(self, property_key, idx):
        """
        property_key (int32): property key symbol
        idx (int): channel
        returns (int) the value
        """
        ret_val = c_int32()
        self.core.SA_CTL_GetProperty_i32(self._id, c_int8(idx), c_uint32(property_key), byref(ret_val), c_size_t(0))
        return ret_val.value

    def GetProperty_i64(self, property_key, idx):
        """
        property_key (int64): property key symbol
        idx (int): channel
        returns (int) the value
        """
        ret_val = c_int64()
        self.core.SA_CTL_GetProperty_i64(self._id, c_int8(idx), c_uint32(property_key), byref(ret_val), c_size_t(0))
        return ret_val.value

    def get_number_of_channels(self):
        return self.GetProperty_i32(SA_CTLDLL.SA_CTL_PKEY_NUMBER_OF_CHANNELS, 0)

    def Reference(self, channel):
        # Reference the controller. Note - this is asynchronous
        self.core.SA_CTL_Reference(self._id, c_int8(channel), c_int8(0))

    def Move(self, pos, channel, moveMode):
        """
        Move to position specified
        pos (float): position to move to
        moveMode (int32): one of the move modes of the controller
            SA_CTLDLL.SA_CTL_MOVE_MODE_CL_ABSOLUTE
            SA_CTLDLL.SA_CTL_MOVE_MODE_CL_RELATIVE
            etc...

        Raises: SA_CTLError if a problem occurs
        """
        # convert pos from m to picometres (the unit used by teh controller)
        pos_pm = int(pos * 1e12)
        self.SetProperty_i32(SA_CTLDLL.SA_CTL_PKEY_MOVE_MODE, channel, moveMode)
        self.core.SA_CTL_Move(self._id, c_int8(channel), c_int64(pos_pm), SA_CTL_TransmitHandle_t(0))

    def Stop(self, channel):
        """
        Stop command sent to the SA_CTL
        """
        logging.debug("Stopping channel %d..." % (channel,))
        self.core.SA_CTL_Stop(self._id, c_int8(channel), c_int8(0))

    # Basic functions

    def _get_channel_state(self, channel):
        """
        Gets the channel state and logs any errors
        channel (int): the channel
        returns (int32): the state
        """

        state = self.GetProperty_i32(SA_CTLDLL.SA_CTL_PKEY_CHANNEL_STATE, channel)
        if state & SA_CTLDLL.SA_CTL_CH_STATE_BIT_MOVEMENT_FAILED:
            if state & SA_CTLDLL.SA_CTL_CH_STATE_BIT_END_STOP_REACHED:
                logging.error("Error in channel %d: End stop reached", channel)
            if state & SA_CTLDLL.SA_CTL_CH_STATE_BIT_RANGE_LIMIT_REACHED:
                logging.error("Error in channel %d: Reached limit", channel)
            if state & SA_CTLDLL.SA_CTL_CH_STATE_BIT_FOLLOWING_LIMIT_REACHED:
                logging.error("Error in channel %d: Following limit reached", channel)
        return state

    def _is_channel_referenced(self, channel):
        """
        Ask the controller if it is referenced.
        """
        return bool(self.GetProperty_i32(SA_CTLDLL.SA_CTL_PKEY_CHANNEL_STATE, channel) | SA_CTLDLL.SA_CTL_CH_STATE_BIT_IS_REFERENCED)

    def _is_channel_moving(self, channel):
        mask = SA_CTLDLL.SA_CTL_CH_STATE_BIT_ACTIVELY_MOVING
        state = self._get_channel_state(channel)
        return bool(state & mask)

    def _get_position(self, channel):
        """
        Get the position on a specified channel
        returns: the position in m (convert from device unit of pm)
        """
        return self.GetProperty_i64(SA_CTLDLL.SA_CTL_PKEY_POSITION, channel) / 1e12

    def _set_speed(self, channel, value):
        """
        Set the speed of the SA_CTL motion
        value: (double) indicating speed for all axes
        """
        logging.debug("Setting speed to %f", value)
        # convert value to pm/s for the controller
        speed = int(value * 1e12)
        self.SetProperty_i64(SA_CTLDLL.SA_CTL_PKEY_MOVE_VELOCITY, channel, speed)

    def _get_speed(self, channel):
        """
        Returns (double) the linear speed of the SA_CTL motion
        """
        # value is given in pm/s
        speed = self.GetProperty_i64(SA_CTLDLL.SA_CTL_PKEY_MOVE_VELOCITY, channel)
        # convert to m/s
        return float(speed) * 1e-12

    def _set_accel(self, channel, value):
        """
        Set the speed of the SA_CTL motion
        value: (double) indicating speed for all axes
        """
        logging.debug("Setting accel to %f", value)
        # convert value to pm/s2 for the controller
        accel = int(value * 1e12)
        self.SetProperty_i64(SA_CTLDLL.SA_CTL_PKEY_MOVE_ACCELERATION, channel, accel)

    def _get_accel(self, channel):
        """
        Returns (float) the accel of the SA_CTL motion
        """
        # value is given in pm/s2
        accel = self.GetProperty_i64(SA_CTLDLL.SA_CTL_PKEY_MOVE_ACCELERATION, channel)
        # convert to m/s
        return float(accel) * 1e-12

    def _set_hold_time(self, channel, hold_time):
        """
        Set the hold time of the channel after the actuator reached the target position
        channel (int): the channel
        hold_time (float): The hold time, in seconds. Use float('inf") for infinte hold time
            or 0 for no hold time
        """
        # hold time is specified in ms in the controller
        if hold_time == float('inf'):
            ht = SA_CTLDLL.SA_CTL_INFINITE
        else:
            ht = int(hold_time * 1e3)

        self.SetProperty_i32(SA_CTLDLL.SA_CTL_PKEY_HOLD_TIME, channel, ht)

    def stop(self, axes=None):
        """
        Stop the SA_CTL controller and update position
        if axes = None, stop all axes
        """
        if axes is None:
            axes = self._axis_map.keys()

        for axis_name in axes:
            self.Stop(self._axis_map.get(axis_name))

        self._updatePosition()

    def _updatePosition(self):
        """
        update the position VA
        """
        p = {}
        try:
            for axis_name, axis_channel in self._axis_map.items():
                p[axis_name] = self._get_position(axis_channel)

        except SA_CTLError as ex:
            if ex.errno == SA_CTLDLL.SA_CTL_ERROR_NOT_REFERENCED:
                logging.warning("Position unknown because SA_CTL is not referenced")
                p = {a: 0 for a in self.axes}
            else:
                raise

        p = self._applyInversion(p)
        logging.debug("Updated position to %s", p)
        self.position._set_value(p, force_write=True)

    def _updateSpeed(self):
        """
        update the speeds
        """
        s = {}
        for axis_name, axis_channel in self._axis_map.items():
            s[axis_name] = self._get_speed(axis_channel)

        logging.debug("Updated speed to %s", s)
        self._speed = s

    def _updateAccel(self):
        """
        update the accels
        """
        a = {}
        for axis_name, axis_channel in self._axis_map.items():
            a[axis_name] = self._get_accel(axis_channel)

        logging.debug("Updated accel to %s", a)
        self._accel = a

    @isasync
    def moveAbs(self, pos):
        if not pos:
            return model.InstantaneousFuture()
        self._checkMoveAbs(pos)
        pos = self._applyInversion(pos)

        f = self._createMoveFuture()
        f = self._executor.submitf(f, self._doMoveAbs, f, pos)
        return f

    @isasync
    def moveRel(self, shift):
        if not shift:
            return model.InstantaneousFuture()
        self._checkMoveRel(shift)
        shift = self._applyInversion(shift)
        f = self._createMoveFuture()
        f = self._executor.submitf(f, self._doMoveRel, f, shift)
        return f

    def _doMoveRel(self, future, pos):
        """
        Blocking and cancellable relative move
        future (Future): the future it handles
        _pos (dict str -> float): axis name -> relative target position
        raise:
            ValueError: if the target position is
            TMCLError: if the controller reported an error
            CancelledError: if cancelled before the end of the move
        """
        with future._moving_lock:
            end = 0  # expected end
            moving_axes = set()
            for an, v in pos.items():
                channel = self._axis_map[an]
                moving_axes.add(channel)
                self.Move(v, channel, SA_CTLDLL.SA_CTL_MOVE_MODE_CL_RELATIVE)
                # compute expected end
                dur = driver.estimateMoveDuration(abs(v),
                                self._speed[an],
                                self._accel[an])

                end = max(time.time() + dur, end)

            self._waitEndMove(future, moving_axes, end)
        logging.debug("move successfully completed")

    def _doMoveAbs(self, future, pos):
        """
        Blocking and cancellable absolute move
        future (Future): the future it handles
        _pos (dict str -> float): axis name -> absolute target position
        raise:
            TMCLError: if the controller reported an error
            CancelledError: if cancelled before the end of the move
        """
        with future._moving_lock:
            end = 0  # expected end
            old_pos = self._applyInversion(self.position.value)
            moving_axes = set()
            for an, v in pos.items():
                channel = self._axis_map[an]
                moving_axes.add(channel)
                self.Move(v, channel, SA_CTLDLL.SA_CTL_MOVE_MODE_CL_ABSOLUTE)
                d = abs(v - old_pos[an])
                dur = driver.estimateMoveDuration(d,
                                                  self._speed[an],
                                                  self._accel[an])
                end = max(time.time() + dur, end)

            self._waitEndMove(future, moving_axes, end)
        logging.debug("move successfully completed")

    def _waitEndMove(self, future, axes, end=0):
        """
        Wait until all the given axes are finished moving, or a request to
        stop has been received.
        future (Future): the future it handles
        axes (set of int): the axes IDs to check
        end (float): expected end time
        raise:
            TimeoutError: if took too long to finish the move
            CancelledError: if cancelled before the end of the move
        """
        moving_axes = set(axes)

        last_upd = time.time()
        dur = max(0.01, min(end - last_upd, 60))
        max_dur = dur * 2 + 1
        logging.debug("Expecting a move of %g s, will wait up to %g s", dur, max_dur)
        timeout = last_upd + max_dur
        last_axes = moving_axes.copy()
        try:
            while not future._must_stop.is_set():
                for channel in moving_axes.copy():  # need copy to remove during iteration
                    if not self._is_channel_moving(channel):
                        moving_axes.discard(channel)
                if not moving_axes:
                    # no more axes to wait for
                    break

                now = time.time()
                if now > timeout:
                    logging.warning("Stopping move due to timeout after %g s.", max_dur)
                    for i in moving_axes:
                        self.Stop(i)
                    raise TimeoutError("Move is not over after %g s, while "
                                       "expected it takes only %g s" %
                                       (max_dur, dur))

                # Update the position from time to time (10 Hz)
                if now - last_upd > 0.1 or last_axes != moving_axes:
                    last_names = set(n for n, i in self._axis_map.items() if i in last_axes)
                    self._updatePosition()
                    last_upd = time.time()
                    last_axes = moving_axes.copy()

                # Wait half of the time left (maximum 0.1 s)
                left = end - time.time()
                sleept = max(0.001, min(left / 2, 0.1))
                future._must_stop.wait(sleept)
            else:
                logging.debug("Move of axes %s cancelled before the end", axes)
                # stop all axes still moving them
                for i in moving_axes:
                    self.Stop(self._axis_map(i))
                future._was_stopped = True
                raise CancelledError()
        finally:
            # TODO: check if the move succeded ? (= Not failed due to stallguard/limit switch)
            self._updatePosition()  # update (all axes) with final position

    def _cancelCurrentMove(self, future):
        """
        Cancels the current move (both absolute or relative). Non-blocking.
        future (Future): the future to stop. Unused, only one future must be
         running at a time.
        return (bool): True if it successfully cancelled (stopped) the move.
        """
        # The difficulty is to synchronise correctly when:
        #  * the task is just starting (not finished requesting axes to move)
        #  * the task is finishing (about to say that it finished successfully)
        logging.debug("Cancelling current move")

        future._must_stop.set()  # tell the thread taking care of the move it's over
        with future._moving_lock:
            if not future._was_stopped:
                logging.debug("Cancelling failed")
            return future._was_stopped

    def _createMoveFuture(self):
        """
        Return (CancellableFuture): a future that can be used to manage a move
        """
        f = CancellableFuture()
        f._moving_lock = threading.Lock()  # taken while moving
        f._must_stop = threading.Event()  # cancel of the current future requested
        f._was_stopped = False  # if cancel was successful
        f.task_canceller = self._cancelCurrentMove
        return f

    @isasync
    def reference(self, axes=None):
        if axes is None:
            # then reference all axes
            axes = self._axis_map.keys()

        f = self._createMoveFuture()
        f = self._executor.submitf(f, self._doReference, f, axes)
        return f

    def _doReference(self, future, axes):
        """
        Actually runs the referencing code
        axes (set of str)
        raise:
            IOError: if referencing failed due to hardware
            CancelledError if was cancelled
        """
        # Reset reference so that if it fails, it states the axes are not
        # referenced (anymore)
        with future._moving_lock:
            try:
                # do the referencing for each axis sequentially
                # (because each referencing is synchronous)
                for a in axes:
                    if future._must_stop.is_set():
                        raise CancelledError()
                    channel = self._axis_map[a]
                    self.referenced._value[a] = False
                    self.Reference(channel)  # search for the negative limit signal to set an origin
                    self._waitEndMove(future, (channel,), time.time() + 100)  # block until it's over
                    self.referenced._value[a] = self._is_channel_referenced(channel)

                    if self.referenced._value[a] == False:
                        pass
                    # TODO: Raise some error here

            except CancelledError:
                # FIXME: if the referencing is stopped, the device refuses to
                # move until referencing is run (and successful).
                # => Need to put back the device into a mode where at least
                # relative moves work.
                logging.warning("Referencing cancelled, device will not move until another referencing")
                future._was_stopped = True
                raise
            except Exception:
                logging.exception("Referencing failure")
                raise
            finally:
                # We only notify after updating the position so that when a listener
                # receives updates both values are already updated.
                self._updatePosition()  # all the referenced axes should be back to 0
                # read-only so manually notify
                self.referenced.notify(self.referenced.value)


class FakeMCS2_DLL(object):
    """
    Fake MCS2 DLL for simulator
    """

    def __init__(self):
        self.properties = {
            SA_CTLDLL.SA_CTL_PKEY_DEVICE_NAME: [0],
            SA_CTLDLL.SA_CTL_PKEY_NUMBER_OF_CHANNELS: [3],
            SA_CTLDLL.SA_CTL_PKEY_MOVE_MODE: [
                    SA_CTLDLL.SA_CTL_MOVE_MODE_CL_ABSOLUTE,
                    SA_CTLDLL.SA_CTL_MOVE_MODE_CL_ABSOLUTE,
                    SA_CTLDLL.SA_CTL_MOVE_MODE_CL_ABSOLUTE,
                    ],
            SA_CTLDLL.SA_CTL_PKEY_CHANNEL_STATE: [0, 0, 0],
            SA_CTLDLL.SA_CTL_PKEY_POSITION: [0, 0, 0],
            SA_CTLDLL.SA_CTL_PKEY_MOVE_VELOCITY: [1, 1, 1],
            SA_CTLDLL.SA_CTL_PKEY_MOVE_ACCELERATION: [1, 1, 1],
            SA_CTLDLL.SA_CTL_PKEY_HOLD_TIME: [0, 0, 0],
        }

        self.target = [0, 0, 0]

        # Specify ranges
        self._range = [(-10e12, 10e12), (-10e12, 10e12), (-10e12, 10e12)]

        self.stopping = threading.Event()

        self._current_move_start = time.time()
        self._current_move_finish = time.time()

    def _pos_in_range(self, ch, pos):
        return (self._range[ch][0] <= pos <= self._range[0][1])

    """
    DLL functions (fake)
    These functions are provided by the real SA_MC DLL
    """

    def SA_CTL_Open(self, id, locator, options):
        logging.debug("sim MCS2: Starting MCS2 Sim")

    def SA_CTL_Close(self, id):
        logging.debug("sim MCS2: Closing MCS2 Sim")

    def SA_CTL_GetFullVersionString(self,):
        return "Fake MCS2 DLL Simulator"

    def SA_CTL_SetProperty_f64(self, handle, ch, property_key, value):
        if not property_key.value in self.properties:
            raise SA_CTLError(SA_CTLDLL.SA_CTL_ERROR_INVALID_KEY)
        self.properties[property_key.value][ch.value] = value.value

    def SA_CTL_SetProperty_i32(self, handle, ch, property_key, value):
        if not property_key.value in self.properties:
            raise SA_CTLError(SA_CTLDLL.SA_CTL_ERROR_INVALID_KEY)
        self.properties[property_key.value][ch.value] = value.value

    def SA_CTL_SetProperty_i64(self, handle, ch, property_key, value):
        if not property_key.value in self.properties:
            raise SA_CTLError(SA_CTLDLL.SA_CTL_ERROR_INVALID_KEY)
        self.properties[property_key.value][ch.value] = value.value

    def SA_CTL_GetProperty_f64(self, handle, ch, property_key, p_val, size):
        if not property_key.value in self.properties:
            raise SA_CTLError(SA_CTLDLL.SA_CTL_ERROR_INVALID_KEY)
        val = _deref(p_val, c_double)
        val.value = self.properties[property_key.value][ch.value]

    def SA_CTL_GetProperty_i32(self, handle, ch, property_key, p_val, size):
        if not property_key.value in self.properties:
            raise SA_CTLError(SA_CTLDLL.SA_CTL_ERROR_INVALID_KEY)

        # Handle movement states before setting the value
        if property_key.value == SA_CTLDLL.SA_CTL_PKEY_CHANNEL_STATE:
            if self.stopping.is_set():  # stopped before move could complete
                # set the position to someplace in between
                self.properties[SA_CTLDLL.SA_CTL_PKEY_POSITION][ch.value] = int((self.target[ch.value] - \
                    self.properties[SA_CTLDLL.SA_CTL_PKEY_POSITION][ch.value]) / 2 + \
                    self.properties[SA_CTLDLL.SA_CTL_PKEY_POSITION][ch.value])
                    
            elif self._current_move_finish < time.time():  # move is finished
                self.properties[SA_CTLDLL.SA_CTL_PKEY_POSITION][ch.value] = \
                    int(self.target[ch.value])
                self.properties[SA_CTLDLL.SA_CTL_PKEY_CHANNEL_STATE][ch.value] &= \
                    ~ (SA_CTLDLL.SA_CTL_CH_STATE_BIT_ACTIVELY_MOVING)
        # update the value of the key
        val = _deref(p_val, c_int32)
        val.value = self.properties[property_key.value][ch.value]

    def SA_CTL_GetProperty_i64(self, handle, ch, property_key, p_val, size):
        if not property_key.value in self.properties:
            raise SA_CTLError(SA_CTLDLL.SA_CTL_ERROR_INVALID_KEY)
        val = _deref(p_val, c_int64)
        val.value = self.properties[property_key.value][ch.value]

    def SA_CTL_Reference(self, handle, ch, _):
        logging.debug("sim MCS2: Referencing channel %d" % (ch.value,))
        property_key = SA_CTLDLL.SA_CTL_PKEY_CHANNEL_STATE
        self.properties[property_key][ch.value] |= SA_CTLDLL.SA_CTL_CH_STATE_BIT_IS_REFERENCED

    def SA_CTL_Move(self, handle, ch, pos_pm, _):
        self.stopping.clear()
        if self._pos_in_range(ch.value, pos_pm.value):
            self._current_move_finish = time.time() + 1.0
            if self.properties[SA_CTLDLL.SA_CTL_PKEY_MOVE_MODE] == SA_CTLDLL.SA_CTL_MOVE_MODE_CL_ABSOLUTE:
                self.target[ch.value] = pos_pm.value
                logging.debug("sim MCS2: Abs move channel %d to %d pm" % (ch.value, pos_pm.value))
            elif self.properties[SA_CTLDLL.SA_CTL_PKEY_MOVE_MODE] == SA_CTLDLL.SA_CTL_MOVE_MODE_CL_RELATIVE:
                self.target[ch.value] = pos_pm.value + self.properties[SA_CTLDLL.SA_CTL_PKEY_POSITION][ch.value]
                logging.debug("sim MCS2: Rel move channel %d to %d pm" % (ch.value, self.target[ch.value]))
            self.properties[SA_CTLDLL.SA_CTL_PKEY_CHANNEL_STATE][ch.value] |= SA_CTLDLL.SA_CTL_CH_STATE_BIT_ACTIVELY_MOVING
        else:
            raise SA_CTLError(SA_CTLDLL.SA_CTL_ERROR_RANGE_LIMIT_REACHED)

    def SA_CTL_Stop(self, handle, ch, _):
        self.stopping.set()
