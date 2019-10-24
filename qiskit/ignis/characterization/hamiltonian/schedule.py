# -*- coding: utf-8 -*-

# This code is part of Qiskit.
#
# (C) Copyright IBM 2019.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""
Schedule generation for measuring hamiltonian parametes
"""

import numpy as np
from qiskit import pulse
from qiskit.pulse import CmdDef, Schedule, PulseError
from qiskit.pulse.channels import Channel
import qiskit.pulse.pulse_lib as pulse_lib
from qiskit.providers import BaseBackend
from typing import List, Tuple, Optional, Union
from math import pi
from qiskit.ignis.characterization import CharacterizationError


def cr_tomography_schedules(c_qubit: int,
                            t_qubit: int,
                            backend: BaseBackend,
                            rabi_schedules: List[Schedule],
                            cmd_def: Optional[CmdDef] = None) -> List[Schedule]:
    """
    Generate `Schedule`s for measuring CR Hamiltonian [1].
    Measuring CR Rabi pulse schedule in all Pauli Basis with control qubit states of both 0 and 1.
    Formatted schedules for the experiment can be generated by helper functions
    `cr1_rabi_schedules` and `cr2_rabi_schedules`, with and without echo sequence.
    This function adds state preparation and measurement schedules to given CR Rabi schedules.
    Partial tomography on target qubit yields information of CR Hamiltonian.
    Generated schedules should be executed with `meas_level=2` or use discriminator to get counts.

    [1] Sheldon, S., Magesan, E., Chow, J. M. & Gambetta, J. M.
    Procedure for systematically tuning up cross-talk in the cross-resonance gate.
    Phys. Rev. A 93, 060302 (2016).

    Args:
        c_qubit: index of control qubit.
        t_qubit: index of target qubit.
        backend: backend object of target system.
        rabi_schedules: CR Rabi schedules to be tomographed.
        cmd_def: command definition of target system if customized.

    Returns:
        schedules: experiments to run.
    """
    defaults = backend.defaults()
    if not cmd_def:
        cmd_def = pulse.CmdDef.from_defaults(defaults.cmd_def, defaults.pulse_library)

    buffer = defaults.buffer

    # pi pulse to flip control qubit
    flip_ctrl = cmd_def.get('x', qubits=c_qubit)
    # measurement and acquisition
    measure = cmd_def.get('measure', qubits=backend.configuration().meas_map[0])

    # schedules to convert measurement axis
    xproj_sched = cmd_def.get('u2', qubits=t_qubit, P0=0, P1=pi)
    yproj_sched = cmd_def.get('u2', qubits=t_qubit, P0=0, P1=0.5*pi)

    proj_delay = max(xproj_sched.duration, yproj_sched.duration) + buffer

    meas_basis = {
        'x': xproj_sched.insert(proj_delay, measure),
        'y': yproj_sched.insert(proj_delay, measure),
        'z': measure.shift(proj_delay)
    }

    schedules = []
    for rabi_sched in rabi_schedules:
        for basis, meas_sched in meas_basis.items():
            for c_state in (0, 1):
                sched = pulse.Schedule(name='%d,%s,%d' % (rabi_sched.name, basis, c_state))
                # flip control qubit
                if c_state:
                    sched = sched.insert(0, flip_ctrl)
                # add cross resonance schedule
                sched = sched.insert(flip_ctrl.duration + buffer, rabi_sched)
                # add measurement
                sched = sched.insert(sched.duration + buffer, meas_sched)

                schedules.append(sched)

    return schedules


def cr1_rabi_schedules(c_qubit: int,
                       t_qubit: int,
                       backend: BaseBackend,
                       cr_samples: List[int],
                       cr_amp: Union[complex, float],
                       sigma: float,
                       risefall: int,
                       cancellation_amp: Optional[Union[complex, float]] = None,
                       cmd_def: Optional[CmdDef] = None) -> Tuple[List[float], List[Schedule]]:
    """
    A helper function to generate CR1 (one pulse CR) Rabi schedules.
    Gaussian flattop (GF) pulse defined in `qiskit.pulse.pulse_lib.gaussian_square` is used.
    Schedule comprises two GFs for CR and cancellation for each pulse duration in `cr_samples`.

    ```
        cr:      ...[GF1(pi/2)]...
        control: .................
        target:  ...[GF2(pi/2)]...
    ```

    All pulse parameters except for `amp` are identical between GFs.

    Args:
        c_qubit: index of control qubit.
        t_qubit: index of target qubit.
        backend: backend object of target system.
        cr_samples: list of cr pulse durations to create Rabi experiments.
        cr_amp: complex amplitude of CR pulse.
        sigma: sigma value of CR pulse edge.
        risefall: duration of CR pulse risefall.
        cancellation_amp: complex amplitude of cancellation pulse (optional).
        cmd_def: command definition of target system if customized.

    Returns:
        cr_times: pulse duration in time unit.
        schedules: CR schedules for Rabi experiment.
    """
    defaults = backend.defaults()
    if not cmd_def:
        cmd_def = pulse.CmdDef.from_defaults(defaults.cmd_def, defaults.pulse_library)

    # get channel instances
    _, t_drive, cr_drive = _get_channels(c_qubit, t_qubit, backend, cmd_def)

    schedules = []
    for index, cr_sample in enumerate(cr_samples):
        sched = pulse.Schedule(name='%d' % index)

        cr_sched = pulse_lib.gaussian_square(duration=cr_sample,
                                             amp=cr_amp,
                                             sigma=sigma,
                                             risefall=risefall)
        if cancellation_amp:
            can_sched = pulse_lib.gaussian_square(duration=cr_sample,
                                                  amp=cancellation_amp,
                                                  sigma=sigma,
                                                  risefall=risefall)
        else:
            can_sched = pulse.commands.Delay(cr_sample)

        sched.insert(0, cr_sched(cr_drive))
        sched.insert(0, can_sched(t_drive))

        schedules.append(sched)

    cr_times = np.array(cr_samples, dtype=np.float) * backend.configuration().dt * 1e-9

    return cr_times, schedules


def cr2_rabi_schedules(c_qubit: int,
                       t_qubit: int,
                       backend: BaseBackend,
                       cr_samples: List[int],
                       cr_amp: Union[complex, float],
                       sigma: float,
                       risefall: int,
                       cancellation_amp: Optional[Union[complex, float]] = None,
                       cmd_def: Optional[CmdDef] = None) -> Tuple[List[float], List[Schedule]]:
    """
    A helper function to generate CR2 (two pulse echoed CR) Rabi schedules.
    Gaussian flattop (GF) pulse defined in `qiskit.pulse.pulse_lib.gaussian_square` is used.
    Schedule comprises four GFs for echoed CR and cancellation
    for each pulse duration in `cr_samples`.
    Two additional Gaussian derivative (GD) pulses are inserted for echo.

    ```
        cr:      ...[GF1(pi/4)]..........[GF1(-pi/4)]............
        control: ...............[GD(pi)]..............[GD(pi)]...
        target:  ...[GF2(pi/4)]..........[FG2(-pi/4)]............
    ```

    All pulse parameters except for `amp` are identical between FGs.

    Args:
        c_qubit: index of control qubit.
        t_qubit: index of target qubit.
        backend: backend object of target system.
        cr_samples: list of cr pulse durations to create Rabi experiments.
        cr_amp: complex amplitude of CR pulse.
        sigma: sigma value of CR pulse edge.
        risefall: duration of CR pulse risefall.
        cancellation_amp: complex amplitude of cancellation pulse (optional).
        cmd_def: command definition of target system if customized.

    Returns:
        cr_times: pulse duration in time unit.
        schedules: CR schedules for Rabi experiment.
    """
    defaults = backend.defaults()
    if not cmd_def:
        cmd_def = pulse.CmdDef.from_defaults(defaults.cmd_def, defaults.pulse_library)

    # get channel instances
    _, t_drive, cr_drive = _get_channels(c_qubit, t_qubit, backend, cmd_def)

    echo_pi = cmd_def.get('x', qubits=c_qubit)
    buffer = backend.defaults().buffer

    schedules = []
    for index, cr_sample in enumerate(cr_samples):
        sched = pulse.Schedule(name='%d' % index)
        half_cr_sample = int(0.5 * cr_sample)

        cr_sched_p = pulse_lib.gaussian_square(duration=half_cr_sample,
                                               amp=cr_amp,
                                               sigma=sigma,
                                               risefall=risefall)
        cr_sched_m = pulse_lib.gaussian_square(duration=half_cr_sample,
                                               amp=-cr_amp,
                                               sigma=sigma,
                                               risefall=risefall)

        if cancellation_amp:
            can_sched_p = pulse_lib.gaussian_square(duration=half_cr_sample,
                                                    amp=cancellation_amp,
                                                    sigma=sigma,
                                                    risefall=risefall)
            can_sched_m = pulse_lib.gaussian_square(duration=half_cr_sample,
                                                    amp=-cancellation_amp,
                                                    sigma=sigma,
                                                    risefall=risefall)
        else:
            can_sched_p = pulse.commands.Delay(half_cr_sample)
            can_sched_m = pulse.commands.Delay(half_cr_sample)

        sched.insert(0, cr_sched_p(cr_drive))
        sched.insert(0, can_sched_p(t_drive))
        sched.insert(sched.duration + buffer, echo_pi)
        sched.insert(sched.duration + buffer, cr_sched_m(cr_drive))
        sched.insert(sched.duration + buffer, can_sched_m(t_drive))
        sched.insert(sched.duration + buffer, echo_pi)

        schedules.append(sched)

    cr_times = np.array(cr_samples, dtype=np.float) * backend.configuration().dt * 1e-9

    return cr_times, schedules


def _get_channels(c_qubit: int,
                  t_qubit: int,
                  backend: BaseBackend,
                  cmd_def: Optional[CmdDef] = None) -> Tuple[Channel, Channel, Channel]:
    """
    A helper function to generate channel instance list.

    Args:
        c_qubit: index of control qubit.
        t_qubit: index of target qubit.
        backend: backend object of target system.
        cmd_def: command definition of target system if customized.

    Returns:
        c_drive: `DriveChannel` instance that drives control qubit.
        t_drive: `DriveChannel` instance that drives target qubit.
        cr_drive: `ControlChannel` instance that drives cross resonance.
    """
    defaults = backend.defaults()
    if not cmd_def:
        cmd_def = pulse.CmdDef.from_defaults(defaults.cmd_def, defaults.pulse_library)

    try:
        cx_ref = cmd_def.get('cx', qubits=(c_qubit, t_qubit))
    except PulseError:
        raise CharacterizationError('Cross resonance is not defined for qubits %d-%d.' % (c_qubit, t_qubit))

    cx_ref = cx_ref.filter(instruction_types=[pulse.commands.PulseInstruction])
    for channel in cx_ref.channels:
        if isinstance(channel, pulse.ControlChannel):
            cr_drive = channel
            break
    else:
        raise CharacterizationError('No valid control channel to drive cross resonance.')
    c_drive = pulse.PulseChannelSpec.from_backend(backend).qubits[c_qubit].drive
    t_drive = pulse.PulseChannelSpec.from_backend(backend).qubits[t_qubit].drive

    return c_drive, t_drive, cr_drive
