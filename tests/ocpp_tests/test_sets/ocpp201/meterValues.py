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

    # -------------------------------------------------------------------------
    # STEP 1 — Expect both connectors Available
    # -------------------------------------------------------------------------
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

    # -------------------------------------------------------------------------
    # STEP 2 — Configure metering controllers
    # -------------------------------------------------------------------------
    r = await charge_point_v201.set_config_variables_req("AlignedDataCtrlr", "Interval", "3")
    assert SetVariableResultType(**r.set_variable_result[0]).attribute_status \
           == SetVariableStatusEnumType.accepted

    r = await charge_point_v201.set_config_variables_req("SampledDataCtrlr", "TxUpdatedInterval", "3")
    assert SetVariableResultType(**r.set_variable_result[0]).attribute_status \
           == SetVariableStatusEnumType.accepted

    r = await charge_point_v201.set_config_variables_req("AlignedDataCtrlr", "SendDuringIdle", "true")
    assert SetVariableResultType(**r.set_variable_result[0]).attribute_status \
           == SetVariableStatusEnumType.accepted

    r = await charge_point_v201.set_config_variables_req("ChargingStation", "PhaseRotation", "TRS")
    assert SetVariableResultType(**r.set_variable_result[0]).attribute_status \
           == SetVariableStatusEnumType.accepted

    # -------------------------------------------------------------------------
    # STEP 3 — Warm‑up: accept 3 MeterValues bursts (idle)
    # -------------------------------------------------------------------------
    logging.debug("Warm-up: collecting MeterValues...")

    for _ in range(3):
        assert await wait_for_and_validate(test_utility, charge_point_v201,
                                           "MeterValues", {"evseId": 1})
        assert await wait_for_and_validate(test_utility, charge_point_v201,
                                           "MeterValues", {"evseId": 2})

    # -------------------------------------------------------------------------
    # STEP 4 — EV plugs in FIRST (IEC 61851 correct sequence)
    # -------------------------------------------------------------------------
    log.info("===== EV plug-in BEFORE authorization =====")
    test_controller.plug_in()
    test_utility.messages.clear()

    # -------------------------------------------------------------------------
    # STEP 5 — Wait for EVConnected (StatusNotification Occupied)
    # -------------------------------------------------------------------------
    log.info("===== Waiting for EVConnected (StatusNotification Occupied) =====")

    await wait_for_and_validate(
        test_utility,
        charge_point_v201,
        "StatusNotification",
        {"connectorStatus": "Occupied", "evseId": 1},
    )

    # -------------------------------------------------------------------------
    # STEP 6 — User swipes RFID token (Authorize)
    # -------------------------------------------------------------------------
    log.info(f"===== SWIPE AUTH: using token {id_tokenJ01.id_token} =====")
    test_controller.swipe(id_tokenJ01.id_token)

    # -------------------------------------------------------------------------
    # STEP 7 — Backend accepts early MeterValues until Started is ready
    # -------------------------------------------------------------------------
    log.info("===== Consuming early MeterValues (3 bursts) =====")

    for _ in range(3):
        await wait_for_and_validate(
            test_utility,
            charge_point_v201,
            "MeterValues",
            {}   # wildcard ANY MeterValues
        )

    # After plug-in and authorization, no more idle MV allowed
    test_utility.forbidden_actions.append("MeterValues")

    # -------------------------------------------------------------------------
    # STEP 8 — Expect TransactionEvent Started
    # -------------------------------------------------------------------------
    log.info("===== Expecting TransactionEvent Started =====")

    assert await wait_for_and_validate(
        test_utility,
        charge_point_v201,
        "TransactionEvent",
        {},  # wildcard Started
    )

    # -------------------------------------------------------------------------
    # STEP 9 — User swipes again to stop the transaction
    # -------------------------------------------------------------------------
    log.info(f"===== SWIPE STOP: using token {id_tokenJ01.id_token} =====")
    test_controller.swipe(id_tokenJ01.id_token)
    test_controller.plug_out()

    # -------------------------------------------------------------------------
    # STEP 10 — Expect TransactionEvent Ended
    # -------------------------------------------------------------------------
    assert await wait_for_and_validate(
        test_utility,
        charge_point_v201,
        "TransactionEvent",
        {},  # wildcard Ended
    )
