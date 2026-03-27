# SPDX-License-Identifier: Apache-2.0
# Copyright ...

# fmt: off
import pytest
from datetime import datetime
import logging
import asyncio

from everest.testing.core_utils.controller.test_controller_interface import TestController
from validations import *
from everest.testing.ocpp_utils.charge_point_utils import wait_for_and_validate, TestUtility
from everest.testing.ocpp_utils.fixtures import *
from everest_test_utils import *
from ocpp.v201.enums import (IdTokenEnumType as IdTokenTypeEnum,
                             SetVariableStatusEnumType,
                             ConnectorStatusEnumType,
                             GetVariableStatusEnumType)
from ocpp.v201.datatypes import *
from ocpp.v201 import call as call201, call_result as call_result201
# fmt: on

log = logging.getLogger("meterValues")


@pytest.mark.asyncio
@pytest.mark.ocpp_version("ocpp2.0.1")
async def test_J01_19(
    central_system_v201: CentralSystem,
    test_controller: TestController,
    test_utility: TestUtility,
):
    """
    J01.FR.19
    ...
    """

    evse_id1 = 1
    connector_id = 1
    evse_id2 = 2

    id_tokenJ01 = IdTokenType(
        id_token="8BADF00D", type=IdTokenTypeEnum.iso14443)

    log.info("##################### J01.FR.19: Sending Meter Values not related to a transaction #################")

    # Reset messages
    test_utility.messages.clear()

    # Start test controller
    test_controller.start()

    # Wait for BootNotification and basic startup
    charge_point_v201 = await central_system_v201.wait_for_chargepoint(
        wait_for_bootnotification=True
    )

    # Check both connectors available
    assert await wait_for_and_validate(
        test_utility,
        charge_point_v201,
        "StatusNotification",
        call201.StatusNotification(
            datetime.now().isoformat(),
            ConnectorStatusEnumType.available,
            evse_id=evse_id1,
            connector_id=connector_id,
        ),
        validate_status_notification_201,
    )
    assert await wait_for_and_validate(
        test_utility,
        charge_point_v201,
        "StatusNotification",
        call201.StatusNotification(
            datetime.now().isoformat(),
            ConnectorStatusEnumType.available,
            evse_id=evse_id2,
            connector_id=connector_id,
        ),
        validate_status_notification_201,
    )

    # Configure controllers
    r = await charge_point_v201.set_config_variables_req("AlignedDataCtrlr", "Interval", "3")
    assert SetVariableResultType(**r.set_variable_result[0]).attribute_status == SetVariableStatusEnumType.accepted

    r = await charge_point_v201.set_config_variables_req("SampledDataCtrlr", "TxUpdatedInterval", "3")
    assert SetVariableResultType(**r.set_variable_result[0]).attribute_status == SetVariableStatusEnumType.accepted

    r = await charge_point_v201.set_config_variables_req("AlignedDataCtrlr", "SendDuringIdle", "true")
    assert SetVariableResultType(**r.set_variable_result[0]).attribute_status == SetVariableStatusEnumType.accepted

    r = await charge_point_v201.set_config_variables_req("ChargingStation", "PhaseRotation", "TRS")
    assert SetVariableResultType(**r.set_variable_result[0]).attribute_status == SetVariableStatusEnumType.accepted

    r = await charge_point_v201.get_config_variables_req("ChargingStation", "PhaseRotation")
    res = GetVariableResultType(**r.get_variable_result[0])
    if res.attribute_status == GetVariableStatusEnumType.accepted:
        log.info(f"Phase Rotation {res.attribute_value}")

    # Collect a few MV idle for warmup
    logging.debug("Collecting meter values...")
    for _ in range(3):
        assert await wait_for_and_validate(test_utility, charge_point_v201, "MeterValues", {"evseId": 1})
        assert await wait_for_and_validate(test_utility, charge_point_v201, "MeterValues", {"evseId": 2})

    # -------------------------------------------------------------------------
    # --- STEP 1: Capture and ignore the automatic dummy token from Everest ---
    # -------------------------------------------------------------------------
    log.info("##################### Ignoring auto token #################")

    # --- Wait for the automatic dummy token published by EVerest ---
    # EVerest may send this token at ANY unpredictable time (0–10 seconds).
    # We MUST wait until we ACTUALLY receive it.
    while True:
        msg = await test_utility.wait_next_message()
        if hasattr(msg, "raw_payload") and "dummy_token_provider" in str(msg.raw_payload):
            # Found the auto token → exit loop
            break

    # Purge message buffer so the auto token is fully ignored
    test_utility.messages.clear()

    # -------------------------------------------------------------------------
    # --- STEP 2: Give time for EVConnected to occur under LAVA (slow) ---
    # -------------------------------------------------------------------------
    log.info("##################### Waiting 5 seconds for EV connection #################")
    await asyncio.sleep(5)

    # -------------------------------------------------------------------------
    # --- STEP 3: User swipes RFID (manual authorization) ---
    # -------------------------------------------------------------------------
    test_controller.swipe(id_tokenJ01.id_token)

    # -------------------------------------------------------------------------
    # --- STEP 4: User plugs in (physical EV connection simulation) ---
    # -------------------------------------------------------------------------
    test_controller.plug_in()

    test_utility.messages.clear()

    # -------------------------------------------------------------------------
    # --- STEP 5: Consume ALL early MeterValues (as backend would do) ---
    # -------------------------------------------------------------------------
    log.info("##################### Consuming early MeterValues #################")

    # LAVA sends typically 3 MeterValues bursts after Occupied.
    # We accept ANY MeterValues regardless of evseId.
    for _ in range(3):
        await wait_for_and_validate(
            test_utility,
            charge_point_v201,
            "MeterValues",
            None
        )

    # Now forbid unexpected MeterValues in the middle of transaction
    test_utility.forbidden_actions.append("MeterValues")

    # -------------------------------------------------------------------------
    # --- STEP 6: Expect TransactionEvent Started ---
    # -------------------------------------------------------------------------
    assert await wait_for_and_validate(
        test_utility,
        charge_point_v201,
        "TransactionEvent",
        {"eventType": "Started"}
    )

    # -------------------------------------------------------------------------
    # --- STEP 7: User swipes again to stop the transaction ---
    # -------------------------------------------------------------------------
    test_controller.swipe(id_tokenJ01.id_token)

    # Physical unplug
    test_controller.plug_out()

    # Expect TransactionEvent Ended
    assert await wait_for_and_validate(
        test_utility,
        charge_point_v201,
        "TransactionEvent",
        {"eventType": "Ended"}
    )
