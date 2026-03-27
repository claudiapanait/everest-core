# SPDX-License-Identifier: Apache-2.0
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
    """
    evse_id1 = 1
    evse_id2 = 2
    connector_id = 1

    # Unknown token for this test
    id_tokenJ01 = IdTokenType(
        id_token="8BADF00D",
        type=IdTokenTypeEnum.iso14443
    )

    log.info("===== J01.FR.19: Sending Meter Values not related to a transaction =====")

    # Clean initial message buffer
    test_utility.messages.clear()

    # Start test controller
    test_controller.start()

    # Wait for BootNotification
    charge_point_v201 = await central_system_v201.wait_for_chargepoint(
        wait_for_bootnotification=True
    )

    # Expect connectors available
    assert await wait_for_and_validate(
        test_utility,
        charge_point_v201,
        "StatusNotification",
        call201.StatusNotification(datetime.now().isoformat(),
                                   ConnectorStatusEnumType.available,
                                   evse_id=evse_id1,
                                   connector_id=connector_id),
        validate_status_notification_201,
    )
    assert await wait_for_and_validate(
        test_utility,
        charge_point_v201,
        "StatusNotification",
        call201.StatusNotification(datetime.now().isoformat(),
                                   ConnectorStatusEnumType.available,
                                   evse_id=evse_id2,
                                   connector_id=connector_id),
        validate_status_notification_201,
    )

    # -------------------------------------------------------------------------
    # --- STEP 1: Capture and ignore the automatic dummy token from EVerest ---
    # -------------------------------------------------------------------------
    log.info("===== Waiting for automatic dummy token (EVerest internal) =====")

    # Explanation:
    # EVerest may publish an automatic dummy token at ANY moment after startup.
    # The timing is unpredictable under LAVA (0–10 seconds).
    # We MUST wait until we *actually receive it* instead of using a fixed sleep.

    while True:
        msg = await test_utility.wait_next_message()

        # Detect messages published by dummy_token_provider
        if hasattr(msg, "raw_payload") and "dummy_token_provider" in str(msg.raw_payload):
            log.info("===== Automatic dummy token detected and ignored =====")
            break

    # Purge this automatic token from test buffer
    test_utility.messages.clear()

    # -------------------------------------------------------------------------
    # --- STEP 2: Configure data sampling controllers ---
    # -------------------------------------------------------------------------

    r = await charge_point_v201.set_config_variables_req("AlignedDataCtrlr", "Interval", "3")
    assert SetVariableResultType(**r.set_variable_result[0]).attribute_status == SetVariableStatusEnumType.accepted

    r = await charge_point_v201.set_config_variables_req("SampledDataCtrlr", "TxUpdatedInterval", "3")
    assert SetVariableResultType(**r.set_variable_result[0]).attribute_status == SetVariableStatusEnumType.accepted

    r = await charge_point_v201.set_config_variables_req("AlignedDataCtrlr", "SendDuringIdle", "true")
    assert SetVariableResultType(**r.set_variable_result[0]).attribute_status == SetVariableStatusEnumType.accepted

    r = await charge_point_v201.set_config_variables_req("ChargingStation", "PhaseRotation", "TRS")
    assert SetVariableResultType(**r.set_variable_result[0]).attribute_status == SetVariableStatusEnumType.accepted

    # Collect PhaseRotation value
    r = await charge_point_v201.get_config_variables_req("ChargingStation", "PhaseRotation")
    res = GetVariableResultType(**r.get_variable_result[0])
    if res.attribute_status == GetVariableStatusEnumType.accepted:
        log.info(f"Phase Rotation: {res.attribute_value}")

    # -------------------------------------------------------------------------
    # --- STEP 3: Stabilize idle MeterValues (warm-up, not part of the test) ---
    # -------------------------------------------------------------------------
    logging.debug("Collecting 3 initial MeterValues (warm-up)")

    for _ in range(3):
        assert await wait_for_and_validate(
            test_utility, charge_point_v201, "MeterValues", {"evseId": 1}
        )
        assert await wait_for_and_validate(
            test_utility, charge_point_v201, "MeterValues", {"evseId": 2}
        )

    # -------------------------------------------------------------------------
    # --- STEP 4: Ensure EVConnected occurs before user swipe ---
    # -------------------------------------------------------------------------
    log.info("===== Waiting 5 seconds to ensure EVConnected (slow LAVA) =====")
    await asyncio.sleep(5)

    # -------------------------------------------------------------------------
    # --- STEP 5: User swipes RFID to authorize ---
    # -------------------------------------------------------------------------
    log.info("===== User swipe to authorize =====")
    test_controller.swipe(id_tokenJ01.id_token)

    # -------------------------------------------------------------------------
    # --- STEP 6: Simulate physical EV plug-in ---
    # -------------------------------------------------------------------------
    test_controller.plug_in()
    test_utility.messages.clear()

    # -------------------------------------------------------------------------
    # --- STEP 7: Consume ALL early MeterValues the backend would accept ---
    # -------------------------------------------------------------------------
    log.info("===== Consuming early MeterValues (3 bursts) =====")

    for _ in range(3):
        await wait_for_and_validate(
            test_utility,
            charge_point_v201,
            "MeterValues",
            None    # Accept ANY payload (evseId=1 or evseId=2)
        )

    # Forbid extra MeterValues after transaction begins
    test_utility.forbidden_actions.append("MeterValues")

    # -------------------------------------------------------------------------
    # --- STEP 8: Expect TransactionEvent Started ---
    # -------------------------------------------------------------------------
    log.info("===== Waiting for TransactionEvent Started =====")
    assert await wait_for_and_validate(
        test_utility,
        charge_point_v201,
        "TransactionEvent",
        {"eventType": "Started"}
    )

    # -------------------------------------------------------------------------
    # --- STEP 9: User swipe to de-authorize (stop) ---
    # -------------------------------------------------------------------------
    test_controller.swipe(id_tokenJ01.id_token)

    # Simulate unplug
    test_controller.plug_out()

    # -------------------------------------------------------------------------
    # --- STEP 10: Expect TransactionEvent Ended ---
    # -------------------------------------------------------------------------
    assert await wait_for_and_validate(
        test_utility,
        charge_point_v201,
        "TransactionEvent",
        {"eventType": "Ended"}
    )
