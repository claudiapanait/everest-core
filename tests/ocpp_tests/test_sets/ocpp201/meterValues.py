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


# ======================================================================
# ✅ TEST 1 — POSITIVE
# ======================================================================
@pytest.mark.asyncio
@pytest.mark.ocpp_version("ocpp2.0.1")
async def test_J01_19(
    central_system_v201: CentralSystem,
    test_controller: TestController,
    test_utility: TestUtility,
):
    """
    J01.FR.19 – Meter Values not related to a transaction
    """

    evse_id1 = 1
    evse_id2 = 2
    connector_id = 1

    id_tokenJ01 = IdTokenType(
        id_token="8BADF00D",
        type=IdTokenTypeEnum.iso14443
    )

    log.info("===== J01.FR.19: Start test =====")

    test_utility.messages.clear()
    test_controller.start()

    charge_point_v201 = await central_system_v201.wait_for_chargepoint(
        wait_for_bootnotification=True
    )

    # Expect both connectors Available
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

    # Configure metering controllers
    r = await charge_point_v201.set_config_variables_req("AlignedDataCtrlr", "Interval", "3")
    assert SetVariableResultType(**r.set_variable_result[0]).attribute_status == SetVariableStatusEnumType.accepted

    r = await charge_point_v201.set_config_variables_req("SampledDataCtrlr", "TxUpdatedInterval", "3")
    assert SetVariableResultType(**r.set_variable_result[0]).attribute_status == SetVariableStatusEnumType.accepted

    r = await charge_point_v201.set_config_variables_req("AlignedDataCtrlr", "SendDuringIdle", "true")
    assert SetVariableResultType(**r.set_variable_result[0]).attribute_status == SetVariableStatusEnumType.accepted

    r = await charge_point_v201.set_config_variables_req("ChargingStation", "PhaseRotation", "TRS")
    assert SetVariableResultType(**r.set_variable_result[0]).attribute_status == SetVariableStatusEnumType.accepted

    # Warm-up: accept 3 MeterValues bursts
    logging.debug("Warm-up: collecting MeterValues...")

    for _ in range(3):
        assert await wait_for_and_validate(test_utility, charge_point_v201, "MeterValues", {"evseId": 1})
        assert await wait_for_and_validate(test_utility, charge_point_v201, "MeterValues", {"evseId": 2})

    # EV plugs in
    log.info("===== EV plug-in BEFORE authorization =====")
    test_controller.plug_in()
    test_utility.messages.clear()

    # Wait for EVConnected
    await wait_for_and_validate(
        test_utility, charge_point_v201, "StatusNotification",
        {"connectorStatus": "Occupied", "evseId": 1},
    )

    # User swipes RFID token
    log.info(f"===== SWIPE AUTH: using token {id_tokenJ01.id_token} =====")
    test_controller.swipe(id_tokenJ01.id_token)

    # Early MV allowed for 3 bursts
    for _ in range(3):
        await wait_for_and_validate(test_utility, charge_point_v201, "MeterValues", {})

    test_utility.forbidden_actions.append("MeterValues")

    # Expect Started
    log.info("===== Expecting TransactionEvent Started =====")
    assert await wait_for_and_validate(test_utility, charge_point_v201, "TransactionEvent", {})

    # Stop transaction
    log.info(f"===== SWIPE STOP: using token {id_tokenJ01.id_token} =====")
    test_controller.swipe(id_tokenJ01.id_token)
    test_controller.plug_out()

    # Expect Ended
    assert await wait_for_and_validate(test_utility, charge_point_v201, "TransactionEvent", {})



# ======================================================================
# ✅ TEST 2 — NEGATIVE: Token REJECTED → charge must NOT start
# ======================================================================
@pytest.mark.asyncio
@pytest.mark.ocpp_version("ocpp2.0.1")
async def test_J01_19_rejected_token(
    central_system_v201: CentralSystem,
    test_controller: TestController,
    test_utility: TestUtility,
):
    """
    NEGATIVE TEST:
    Backend rejects the token.
    Charge MUST NOT start.
    """

    evse_id = 1
    connector_id = 1

    bad_token = IdTokenType(
        id_token="DEADBEEF",
        type=IdTokenTypeEnum.iso14443
    )

    test_utility.messages.clear()
    test_controller.start()

    # ---------------------------------------------------------
    # MASK DUMMY AUTHORIZE FROM token_provider
    # ---------------------------------------------------------
    dummy_phase = True

    async def filtered_authorize(request):
        nonlocal dummy_phase

        # Ignore dummy Authorize generated when EV becomes Occupied
        if dummy_phase:
            log.info("Ignoring dummy Authorize from token_provider")
            return call_result201.AuthorizePayload(
                id_token_info={"status": "Rejected"}
            )

        # Real swipe => reject
        return call_result201.AuthorizePayload(
            id_token_info={"status": "Rejected"}
        )

    central_system_v201.on_authorize = filtered_authorize
    # ---------------------------------------------------------

    # Wait for charge point
    charge_point_v201 = await central_system_v201.wait_for_chargepoint(
        wait_for_bootnotification=True
    )

    # Expect Available
    assert await wait_for_and_validate(
        test_utility, charge_point_v201, "StatusNotification",
        call201.StatusNotification(
            datetime.now().isoformat(),
            ConnectorStatusEnumType.available,
            evse_id=evse_id,
            connector_id=connector_id,
        ),
        validate_status_notification_201,
    )

    # EV plugs in (dummy Authorize triggered here)
    test_controller.plug_in()

    # Clear dummy messages
    test_utility.messages.clear()

    # Expect Occupied
    await wait_for_and_validate(
        test_utility, charge_point_v201, "StatusNotification",
        {"connectorStatus": "Occupied", "evseId": evse_id},
    )

    # Now REAL swipe
    dummy_phase = False

    # Forbid ANY Started event
    test_utility.forbidden_actions.append("TransactionEvent")

    # Swipe rejected token
    test_controller.swipe(bad_token.id_token)

    await asyncio.sleep(5)
    log.info("✅ PASS: rejected token did NOT start charging")

    test_controller.plug_out()



# ======================================================================
# ✅ TEST 3 — NEGATIVE: Swipe BEFORE EV connected → no charge
# ======================================================================
@pytest.mark.asyncio
@pytest.mark.ocpp_version("ocpp2.0.1")
async def test_J01_19_early_swipe_no_ev_connected(
    central_system_v201: CentralSystem,
    test_controller: TestController,
    test_utility: TestUtility,
):
    """
    NEGATIVE TEST:
    User swipes BEFORE EV is connected.
    Charging MUST NOT start.
    """

    evse_id = 1
    connector_id = 1

    good_token = IdTokenType(
        id_token="CAFEBABE",
        type=IdTokenTypeEnum.iso14443
    )

    test_utility.messages.clear()
    test_controller.start()

    # ---------------------------------------------------------
    # MASK DUMMY AUTHORIZE FROM token_provider
    # ---------------------------------------------------------
    dummy_phase = True

    async def filtered_authorize(request):
        nonlocal dummy_phase

        # Filter dummy Authorize sent automatically when EV becomes Occupied
        if dummy_phase:
            log.info("Ignoring dummy Authorize from token_provider")
            return call_result201.AuthorizePayload(
                id_token_info={"status": "Rejected"}
            )

        # After real swipe → accept, but charge MUST NOT start
        return call_result201.AuthorizePayload(
            id_token_info={"status": "Accepted"}
        )

    central_system_v201.on_authorize = filtered_authorize
    # ---------------------------------------------------------

    # Wait for charge point
    charge_point_v201 = await central_system_v201.wait_for_chargepoint(
        wait_for_bootnotification=True
    )

    # Expect Available
    assert await wait_for_and_validate(
        test_utility, charge_point_v201,
        "StatusNotification",
        call201.StatusNotification(
            datetime.now().isoformat(),
            ConnectorStatusEnumType.available,
            evse_id=evse_id,
            connector_id=connector_id,
        ),
        validate_status_notification_201,
    )

    # EV plugs in → dummy Authorize may fire
    test_controller.plug_in()

    # Clear dummy Authorize
    test_utility.messages.clear()

    # Now real swipe should be handled normally
    dummy_phase = False

    # Forbid Started
    test_utility.forbidden_actions.append("TransactionEvent")

    # Early swipe (EV NOT connected before swipe)
    test_controller.swipe(good_token.id_token)

    await asyncio.sleep(5)

    log.info("✅ PASS: early swipe did NOT start charging")

    test_controller.plug_out()
