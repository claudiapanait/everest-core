#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2020 - 2022 Pionix GmbH and Contributors to EVerest

import logging

# --- Local logging setup (same pattern as your original basic test) ---
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

logging.basicConfig(
    level=logging.DEBUG,
)
# ---------------------------------------------------------------------

import pytest
import time
import threading
import queue
from enum import Enum

# --- Silence ONLY the 'Unknown pytest.mark.everest_core_config' warning locally ---
import warnings
from _pytest.warning_types import PytestUnknownMarkWarning

warnings.filterwarnings(
    "ignore",
    category=PytestUnknownMarkWarning,
    message=r"Unknown pytest\.mark\.everest_core_config"
)
# ----------------------------------------------------------------------------------

from everest.testing.core_utils.fixtures import *
from everest.testing.core_utils.everest_core import EverestCore, Requirement
from everest.framework import Module, RuntimeSession


class Mode(Enum):
    """Charging modes supported by this test suite."""
    Basic = 0  # IEC 61851 PWM-based charging
    HLC_AC = 1  # ISO15118-2 AC (High Level Communication)
    HLC_DC = 2  # ISO15118-2 DC (High Level Communication)


class ProbeModule:
    """
    Thin wrapper around the 'probe' module to:
      - subscribe to EVSE session events,
      - drive the EV simulator (car_simulator),
      - collect imported energy from the EVSE.
    """

    def __init__(self, session: RuntimeSession):
        m = Module('probe', session)
        self._setup = m.say_hello()

        # Subscribe to EVSE session events
        evse_manager_ff = self._setup.connections['connector_1'][0]
        m.subscribe_variable(evse_manager_ff, 'session_event', self._handle_evse_manager_event)

        self._msg_queue = queue.Queue()
        self._energy_wh_import = 0
        self._ready_event = threading.Event()
        self._mod = m
        self._all_events = []  # Track all events

        # Signal readiness once 'probe' finished its init path
        m.init_done(self._ready)

    def _ready(self):
        logging.info("Probe module reports: READY.")
        self._ready_event.set()

    def _handle_evse_manager_event(self, args):
        """
        Callback invoked on each EVSE 'session_event'.
        Stores the event in a queue and extracts imported energy on transaction end.
        """
        event = args["event"]
        logging.info(f"EVSE event received: {event}")
        self._msg_queue.put(event)
        self._all_events.append(event)

        if 'transaction_finished' in args:
            energy = args['transaction_finished']['meter_value']['energy_Wh_import']['total']
            logging.info(f"Energy imported reported by EVSE: {energy} Wh")
            self._energy_wh_import = energy

    def test(self, timeout: float, mode: Mode, cmd_string: str = None) -> bool:
        """
        Run a charging simulation via the 'test_control' interface.
        Success criteria:
          - 'TransactionStarted' then 'TransactionFinished' received in that order,
          - imported energy > 0 Wh.
        """
        end_of_time = time.time() + timeout

        logging.info("Waiting for probe module READY…")
        if not self._ready_event.wait(timeout):
            logging.error("Timeout: probe never became ready.")
            return False

        # Retrieve the 'test_control' fulfillment
        car_sim_ff = self._setup.connections['test_control'][0]

        # Enable EV simulator
        logging.info("Enabling EV simulator…")
        self._mod.call_command(car_sim_ff, 'enable', {'value': True})

        # Select scenario (use provided cmd_string if available)
        if cmd_string is None:
            if mode == Mode.Basic:
                # Classic IEC 61851 PWM-based AC session
                cmd_string = (
                    "sleep 1;"
                    "iec_wait_pwr_ready;"
                    "sleep 1;"
                    "draw_power_regulated 16,3;"
                    "sleep 20;"
                    "unplug"
                )
            elif mode == Mode.HLC_AC:
                # ISO15118-2 AC HLC session
                cmd_string = (
                    "sleep 1;"
                    "iso_wait_slac_matched;"
                    "iso_start_v2g_session AC;"
                    "iso_wait_pwr_ready;"
                    "iso_draw_power_regulated 16,3;"
                    "iso_wait_for_stop 20;"
                    "iso_wait_v2g_session_stopped;"
                    "unplug"
                )
            elif mode == Mode.HLC_DC:
                # ISO15118-2 DC HLC session
                cmd_string = (
                    "sleep 1;"
                    "iso_wait_slac_matched;"
                    "iso_start_v2g_session DC;"
                    "iso_wait_pwr_ready;"
                    "iso_wait_for_stop 20;"
                    "iso_wait_v2g_session_stopped;"
                    "unplug"
                )
            else:
                logging.error("Unknown charging mode.")
                return False

        logging.info(f"Charging command to simulator: {cmd_string}")

        # Start the simulation
        logging.info("Sending charging command to simulator…")
        self._mod.call_command(car_sim_ff, 'execute_charging_session', {'value': cmd_string})

        expected_events = ['TransactionStarted', 'TransactionFinished']
        logging.info("Waiting for EVSE events (TransactionStarted -> TransactionFinished)…")

        while expected_events:
            time_left = end_of_time - time.time()
            if time_left < 0:
                logging.error("Timeout: expected event not received.")
                return False

            try:
                ev = self._msg_queue.get(timeout=time_left)
                logging.info(f"Event received: {ev}")
                if ev == expected_events[0]:
                    logging.info(f"Expected event matched: {ev}")
                    expected_events.pop(0)
            except queue.Empty:
                logging.error("Timeout while waiting for EVSE event")
                return False

        logging.info(f"Total energy import reported: {self._energy_wh_import} Wh")
        return self._energy_wh_import > 0


@pytest.mark.everest_core_config('config-sil.yaml')
@pytest.mark.asyncio
async def test_hlc_ac_charging(everest_core: EverestCore):
    """
    Launch EVerest with 'probe' and perform an ISO15118‑2 AC (HLC) charging test.
    """
    logging.info(">>>>>>>>>> HLC ISO15118-2 AC TEST START <<<<<<<<<<")

    test_connections = {
        'test_control': [Requirement('ev_manager', 'main')],
        'connector_1': [Requirement('connector_1', 'evse')],
    }

    logging.info("Starting EVerest with PROBE module (HLC AC)…")
    everest_core.start(standalone_module='probe', test_connections=test_connections)

    # Create runtime session
    session = RuntimeSession(
        str(everest_core.prefix_path),
        str(everest_core.everest_config_path)
    )
    probe = ProbeModule(session)

    logging.info("Waiting for EVerest modules to start…")
    if everest_core.status_listener.wait_for_status(18, ["ALL_MODULES_STARTED"]):
        everest_core.all_modules_started_event.set()
        logging.info("EVerest core reports: ALL MODULES STARTED.")

    assert probe.test(90, Mode.HLC_AC)
    assert probe._energy_wh_import > 0, "No energy was imported"

    logging.info(">>>>>>>>>> HLC ISO15118-2 AC TEST PASSED <<<<<<<<<<")


@pytest.mark.everest_core_config('config-sil.yaml')
@pytest.mark.asyncio
async def test_hlc_ac_charging_with_early_disconnect(everest_core: EverestCore):
    """
    Test handling of early disconnection during AC-HLC
    """
    logging.info(">>>>>>>>>> AC HLC EARLY DISCONNECT TEST START <<<<<<<<<<")

    test_connections = {
        'test_control': [Requirement('ev_manager', 'main')],
        'connector_1': [Requirement('connector_1', 'evse')],
    }

    everest_core.start(standalone_module='probe', test_connections=test_connections)
    session = RuntimeSession(
        str(everest_core.prefix_path),
        str(everest_core.everest_config_path)
    )
    probe = ProbeModule(session)

    if everest_core.status_listener.wait_for_status(18, ["ALL_MODULES_STARTED"]):
        everest_core.all_modules_started_event.set()

    cmd_string = (
        "sleep 1;"
        "iso_wait_slac_matched;"
        "iso_start_v2g_session AC;"
        "iso_wait_pwr_ready;"
        "iso_draw_power_regulated 16,3;"
        "sleep 2;"
        "iso_stop_charging;"
        "iso_wait_v2g_session_stopped;"
        "unplug"
    )
    # Test should handle early disconnect gracefully
    # Verify transaction events are still properly recorded
    assert probe.test(60, Mode.HLC_AC, cmd_string)
    assert probe._energy_wh_import > 0, "No energy was imported"

    logging.info(">>>>>>>>>> AC HLC EARLY DISCONNECT TEST PASSED <<<<<<<<<<")


@pytest.mark.everest_core_config('config-sil-ac-1phase.yaml')
@pytest.mark.asyncio
async def test_hlc_ac_charging_one_phase(everest_core: EverestCore, caplog):
    """
    Test AC HLC with one-phase power delivery
    """
    logging.info(">>>>>>>>>> AC HLC ONE-PHASE TEST START <<<<<<<<<<")
    caplog.set_level(logging.DEBUG)

    test_connections = {
        'test_control': [Requirement('ev_manager', 'main')],
        'connector_1': [Requirement('connector_1', 'evse')],
    }

    everest_core.start(standalone_module='probe', test_connections=test_connections)
    session = RuntimeSession(
        str(everest_core.prefix_path),
        str(everest_core.everest_config_path)
    )
    probe = ProbeModule(session)

    if everest_core.status_listener.wait_for_status(18, ["ALL_MODULES_STARTED"]):
        everest_core.all_modules_started_event.set()

    cmd_string = (
        "sleep 1;"
        "iso_wait_slac_matched;"
        "iso_start_v2g_session AC;"  
        "iso_wait_pwr_ready;"
        "iso_draw_power_regulated 16,1;"
        "iso_wait_for_stop 20;"
        "iso_wait_v2g_session_stopped;"
        "unplug"
    )
    result = probe.test(120, Mode.HLC_AC, cmd_string)
    assert result, "Charging test failed"
    assert probe._energy_wh_import > 0, "No energy was imported"
    logging.info("Check logs for '3ph/1ph: Switching #ph from 3 to 1'")
    phase_switch_log = "3ph/1ph: Switching #ph from 3 to 1"

    # Search in caplog.text (all captured log output as a single string)
    if phase_switch_log in caplog.text:
        logging.info(f"Phase switching log verified: '{phase_switch_log}'")
    else:
        logging.error(f"Phase switching log NOT found: '{phase_switch_log}'")
        assert False

    logging.info(">>>>>>>>>> AC HLC ONE-PHASE TEST PASSED <<<<<<<<<<")


@pytest.mark.everest_core_config('config-sil.yaml')
@pytest.mark.asyncio
async def test_hlc_ac_charging_with_pause_resume(everest_core: EverestCore):
    """
    Test pausing and resuming charging during AC-HLC session
    """
    logging.info(">>>>>>>>>> AC HLC PAUSE/RESUME TEST START <<<<<<<<<<")

    test_connections = {
        'test_control': [Requirement('ev_manager', 'main')],
        'connector_1': [Requirement('connector_1', 'evse')],
    }

    everest_core.start(standalone_module='probe', test_connections=test_connections)
    session = RuntimeSession(
        str(everest_core.prefix_path),
        str(everest_core.everest_config_path)
    )
    probe = ProbeModule(session)

    if everest_core.status_listener.wait_for_status(18, ["ALL_MODULES_STARTED"]):
        everest_core.all_modules_started_event.set()

    cmd_string = (
        "sleep 1;"
        "iso_wait_slac_matched;"
        "iso_start_v2g_session AC;"
        "iso_wait_pwr_ready;"
        "iso_draw_power_regulated 16,3;"
        "sleep 5;"
        "iso_pause_charging;"
        "sleep 3;"
        "iso_wait_for_resume 15;"
        "iso_wait_for_stop 15;"
        "iso_wait_v2g_session_stopped;"
        "unplug"
    )

    assert probe.test(90, Mode.HLC_AC, cmd_string)
    assert probe._energy_wh_import > 0, "No energy was imported"

    logging.info(f"Events received: {probe._all_events}")

    assert 'ChargingPausedEV' in probe._all_events, "ChargingPausedEV event missing!"
    assert 'ChargingResumed' in probe._all_events, "ChargingResumed event missing!"

    logging.info(">>>>>>>>>> AC HLC PAUSE/RESUME TEST PASSED <<<<<<<<<<")
