import logging
import time

from tests.common.helpers.assertions import pytest_assert
from tests.common.fixtures.conn_graph_facts import conn_graph_facts,\
    fanout_graph_facts  # noqa: F401
from tests.common.snappi_tests.snappi_helpers import get_dut_port_id
from tests.common.snappi_tests.common_helpers import pfc_class_enable_vector,\
    get_lossless_buffer_size, get_pg_dropped_packets,\
    stop_pfcwd, disable_packet_aging, sec_to_nanosec,\
    get_pfc_frame_count, packet_capture, config_capture_pkt,\
    traffic_flow_mode, calc_pfc_pause_flow_rate      # noqa: F401
from tests.common.snappi_tests.port import select_ports, select_tx_port  # noqa: F401
from tests.common.snappi_tests.snappi_helpers import wait_for_arp  # noqa: F401
from tests.common.snappi_tests.traffic_generation import setup_base_traffic_config, generate_test_flows, \
    generate_pause_flows, run_traffic, verify_basic_test_flow
from tests.common.snappi_tests.snappi_test_params import SnappiTestParams
from tests.common.snappi_tests.read_pcap import validate_pfc_frame_cisco

logger = logging.getLogger(__name__)

dut_port_config = []
PAUSE_FLOW_NAME = 'Pause Storm'
TEST_FLOW_NAME = 'Test Flow'
TEST_FLOW_AGGR_RATE_PERCENT = 45
data_flow_pkt_size = 1024
DATA_FLOW_DURATION_SEC = 5
data_flow_delay_sec = 1
TOLERANCE_THRESHOLD = 0.05
ANSIBLE_POLL_DELAY_SEC = 4


def run_pfc_valid_src_mac_test(
        api,
        testbed_config,
        port_config_list,
        conn_data,
        fanout_data,
        duthost,
        dut_port,
        global_pause,
        pause_prio_list,
        test_prio_list,
        prio_dscp_map,
        test_traffic_pause,
        snappi_extra_params=None):
    """
    Run a PFC test
    Args:
        api (obj): snappi session
        testbed_config (obj): testbed L1/L2/L3 configuration
        port_config_list (list): list of port configuration
        conn_data (dict): the dictionary returned by conn_graph_fact.
        fanout_data (dict): the dictionary returned by fanout_graph_fact.
        duthost (Ansible host instance): device under test
        dut_port (str): DUT port to test
        global_pause (bool): if pause frame is IEEE 802.3X pause
        pause_prio_list (list): priorities to pause for pause frames
        test_prio_list (list): priorities of test flows
        prio_dscp_map (dict): Priority vs. DSCP map (key = priority).
        test_traffic_pause (bool): if test flows are expected to be paused
        snappi_extra_params (SnappiTestParams obj): additional parameters for Snappi traffic

    Returns:
        N/A
    """

    pytest_assert(testbed_config is not None, 'Fail to get L2/3 testbed config')

    if snappi_extra_params is None:
        snappi_extra_params = SnappiTestParams()

    stop_pfcwd(duthost)
    disable_packet_aging(duthost)
    global DATA_FLOW_DURATION_SEC
    global data_flow_delay_sec

    # Get the ID of the port to test
    port_id = get_dut_port_id(dut_hostname=duthost.hostname,
                              dut_port=dut_port,
                              conn_data=conn_data,
                              fanout_data=fanout_data)

    pytest_assert(port_id is not None,
                  'Fail to get ID for port {}'.format(dut_port))

    # Rate percent must be an integer
    test_flow_rate_percent = int(TEST_FLOW_AGGR_RATE_PERCENT / len(test_prio_list))

    # Generate base traffic config
    snappi_extra_params.base_flow_config = setup_base_traffic_config(testbed_config=testbed_config,
                                                                     port_config_list=port_config_list,
                                                                     port_id=port_id)

    speed_str = testbed_config.layer1[0].speed
    speed_gbps = int(speed_str.split('_')[1])

    if snappi_extra_params.poll_device_runtime:
        # If the switch needs to be polled as traffic is running for stats,
        # then the test runtime needs to be increased for the polling delay
        DATA_FLOW_DURATION_SEC += ANSIBLE_POLL_DELAY_SEC
        data_flow_delay_sec = ANSIBLE_POLL_DELAY_SEC

    if snappi_extra_params.packet_capture_type != packet_capture.NO_CAPTURE:
        # Setup capture config
        if snappi_extra_params.is_snappi_ingress_port_cap:
            # packet capture is required on the ingress snappi port
            snappi_extra_params.packet_capture_ports = [snappi_extra_params.base_flow_config["rx_port_name"]]
        else:
            # packet capture will be on the egress snappi port
            snappi_extra_params.packet_capture_ports = [snappi_extra_params.base_flow_config["tx_port_name"]]

        snappi_extra_params.packet_capture_file = snappi_extra_params.packet_capture_type.value

        config_capture_pkt(testbed_config=testbed_config,
                           port_names=snappi_extra_params.packet_capture_ports,
                           capture_type=snappi_extra_params.packet_capture_type,
                           capture_name=snappi_extra_params.packet_capture_file)
        logger.info("Packet capture file: {}.pcapng".format(snappi_extra_params.packet_capture_file))

    # Set default traffic flow configs if not set
    if snappi_extra_params.traffic_flow_config.data_flow_config is None:
        snappi_extra_params.traffic_flow_config.data_flow_config = {
            "flow_name": TEST_FLOW_NAME,
            "flow_dur_sec": DATA_FLOW_DURATION_SEC,
            "flow_rate_percent": test_flow_rate_percent,
            "flow_rate_pps": None,
            "flow_rate_bps": None,
            "flow_pkt_size": data_flow_pkt_size,
            "flow_pkt_count": None,
            "flow_delay_sec": data_flow_delay_sec,
            "flow_traffic_type": traffic_flow_mode.FIXED_DURATION
        }

    if snappi_extra_params.traffic_flow_config.pause_flow_config is None:
        snappi_extra_params.traffic_flow_config.pause_flow_config = {
            "flow_name": PAUSE_FLOW_NAME,
            "flow_dur_sec": None,
            "flow_rate_percent": None,
            "flow_rate_pps": calc_pfc_pause_flow_rate(speed_gbps),
            "flow_rate_bps": None,
            "flow_pkt_size": 64,
            "flow_pkt_count": None,
            "flow_delay_sec": 0,
            "flow_traffic_type": traffic_flow_mode.CONTINUOUS
        }

    if snappi_extra_params.packet_capture_type == packet_capture.PFC_CAPTURE:
        # PFC pause frame capture is requested
        validate_pfc_frame = True
    else:
        # PFC pause frame capture is not requested
        validate_pfc_frame = False

    # Generate test flow config
    generate_test_flows(testbed_config=testbed_config,
                        test_flow_prio_list=test_prio_list,
                        prio_dscp_map=prio_dscp_map,
                        snappi_extra_params=snappi_extra_params)

    # Generate pause storm config
    generate_pause_flows(testbed_config=testbed_config,
                         pause_prio_list=pause_prio_list,
                         global_pause=global_pause,
                         snappi_extra_params=snappi_extra_params)

    flows = testbed_config.flows

    all_flow_names = [flow.name for flow in flows]
    data_flow_names = [flow.name for flow in flows if PAUSE_FLOW_NAME not in flow.name]

    # Clear PFC, queue and interface counters before traffic run
    duthost.command(" sonic-clear pfccounters")
    duthost.command("sonic-clear queuecounters")
    duthost.command("sonic-clear counters")
    time.sleep(1)

    """ Run traffic """
    tgen_flow_stats, _, _ = run_traffic(
                                        duthost=duthost,
                                        api=api,
                                        config=testbed_config,
                                        data_flow_names=data_flow_names,
                                        all_flow_names=all_flow_names,
                                        exp_dur_sec=DATA_FLOW_DURATION_SEC +
                                        data_flow_delay_sec,
                                        snappi_extra_params=snappi_extra_params)

    # Reset pfc delay parameter
    pfc = testbed_config.layer1[0].flow_control.ieee_802_1qbb
    pfc.pfc_delay = 0

    # Verify PFC pause frames
    if validate_pfc_frame:
        peer_mac_addr = snappi_extra_params.base_flow_config["rx_port_config"].gateway_mac
        is_valid_pfc_frame, error_msg = validate_pfc_frame_cisco(
                        snappi_extra_params.packet_capture_file + ".pcapng",
                        peer_mac_addr=peer_mac_addr)
        pytest_assert(is_valid_pfc_frame, error_msg)
        return

    # Verify basic test flows metrics from ixia
    verify_basic_test_flow(flow_metrics=tgen_flow_stats,
                           speed_gbps=speed_gbps,
                           tolerance=TOLERANCE_THRESHOLD,
                           test_flow_pause=test_traffic_pause,
                           snappi_extra_params=snappi_extra_params)
