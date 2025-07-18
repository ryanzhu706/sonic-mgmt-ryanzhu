import logging
import pytest
import time
import re

from tests.common.fixtures.conn_graph_facts import enum_fanout_graph_facts      # noqa: F401
from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.pfc_storm import PFCStorm
from tests.common.helpers.pfcwd_helper import start_wd_on_ports, start_background_traffic     # noqa: F401

from tests.common.plugins.loganalyzer import DisableLogrotateCronContext
from tests.common.helpers.pfcwd_helper import send_background_traffic
from tests.common import config_reload

pytestmark = [
    pytest.mark.topology('any')
]

ITERATION_NUM = 20

logger = logging.getLogger(__name__)


@pytest.fixture(scope="class")
def pfc_queue_idx(pfcwd_timer_setup_restore):
    # This is used by the common code, this needs to be defined
    # before using start_background_traffic() fixture.
    yield pfcwd_timer_setup_restore['storm_handle'].pfc_queue_idx


@pytest.fixture(autouse=True)
def ignore_loganalyzer_exceptions(enum_rand_one_per_hwsku_frontend_hostname, loganalyzer):
    """
    Fixture that ignores expected failures during test execution.

    Args:
        duthost (AnsibleHost): DUT instance
        loganalyzer (loganalyzer): Loganalyzer utility fixture
    """
    if loganalyzer:
        ignoreRegex = [
            (".*ERR syncd#syncd: :- process_on_fdb_event: "
             "invalid OIDs in fdb notifications, NOT translating and NOT storing in ASIC DB.*"),
            (".*ERR syncd#syncd: :- process_on_fdb_event: "
             "FDB notification was not sent since it contain invalid OIDs, bug.*")
        ]
        loganalyzer[enum_rand_one_per_hwsku_frontend_hostname].ignore_regex.extend(ignoreRegex)

    yield


@pytest.fixture(scope='module', autouse=True)
def pfcwd_timer_setup_restore(setup_pfc_test, enum_fanout_graph_facts, duthosts,        # noqa: F811
                              enum_rand_one_per_hwsku_frontend_hostname, fanouthosts):
    """
    Fixture that inits the test vars, start PFCwd on ports and cleans up after the test run

    Args:
        setup_pfc_test (fixture): module scoped, autouse PFC fixture
        enum_fanout_graph_facts (fixture): fanout graph info
        duthost (AnsibleHost): DUT instance
        fanouthosts (AnsibleHost): fanout instance

    Yields:
        timers (dict): pfcwd timer values
        storm_handle (PFCStorm): class PFCStorm instance
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    asic_type = duthost.facts['asic_type']
    logger.info("--- Pfcwd timer test setup ---")
    setup_info = setup_pfc_test
    test_ports = setup_info['test_ports']
    timers = setup_info['pfc_timers']
    eth0_ip = setup_info['eth0_ip']
    # In Python2, dict.keys() returns list object, but in Python3 returns an iterable but not indexable object.
    # So that convert to list explicitly.
    pfc_wd_test_port = list(test_ports.keys())[0]
    neighbors = setup_info['neighbors']
    fanout_info = enum_fanout_graph_facts
    dut = duthost
    fanout = fanouthosts
    peer_params = populate_peer_info(asic_type, neighbors, fanout_info, pfc_wd_test_port)
    storm_handle = set_storm_params(dut, fanout_info, fanout, peer_params)
    timers['pfc_wd_restore_time'] = 400
    start_wd_on_ports(dut, pfc_wd_test_port, timers['pfc_wd_restore_time'],
                      timers['pfc_wd_detect_time'])
    # enable routing from mgmt interface to localhost
    dut.sysctl(name="net.ipv4.conf.eth0.route_localnet", value=1, sysctl_set=True)
    # rule to forward syslog packets from mgmt interface to localhost
    syslog_ip = duthost.get_rsyslog_ipv4()
    dut.iptables(action="insert", chain="PREROUTING", table="nat", protocol="udp",
                 destination=eth0_ip, destination_port=514, jump="DNAT",
                 to_destination="{}:514".format(syslog_ip))

    logger.info("--- Pfcwd Timer Testrun ---")
    yield {'timers': timers,
           'storm_handle': storm_handle,
           'test_ports': test_ports,
           'selected_test_port': pfc_wd_test_port
           }

    logger.info("--- Pfcwd timer test cleanup ---")
    # clear pfcwd stats and reset to default for next run
    config_reload(duthost, safe_reload=True, check_intf_up_ports=True, wait_for_bgp=True)
    dut.iptables(table="nat", flush="yes")
    dut.sysctl(name="net.ipv4.conf.eth0.route_localnet", value=0, sysctl_set=True)
    storm_handle.stop_storm()


def populate_peer_info(asic_type, neighbors, fanout_info, port):
    """
    Build the peer_info map which will be used by the storm generation class

    Args:
        neighbors (dict): fanout info for each DUT port
        fanout_info (dict): fanout graph info
        port (string): test port

    Returns:
        peer_info (dict): all PFC params needed for fanout for storm generation
    """
    if asic_type == 'vs':
        return {}
    peer_dev = neighbors[port]['peerdevice']
    peer_port = neighbors[port]['peerport']
    peer_info = {'peerdevice': peer_dev,
                 'hwsku': fanout_info[peer_dev]['device_info']['HwSku'],
                 'pfc_fanout_interface': peer_port
                 }
    return peer_info


def set_storm_params(dut, fanout_info, fanout, peer_params):
    """
    Setup storm parameters

    Args:
        dut (AnsibleHost): DUT instance
        fanout_info (fixture): fanout graph info
        fanout (AnsibleHost): fanout instance
        peer_params (dict): all PFC params needed for fanout for storm generation

    Returns:
        storm_handle (PFCStorm): class PFCStorm intance
    """
    logger.info("Setting up storm params")
    pfc_queue_index = 4
    pfc_frames_count = 1000000
    peer_device = peer_params['peerdevice'] if dut.facts['asic_type'] != 'vs' else ""
    if dut.topo_type == 't2' and fanout[peer_device].os == 'sonic':
        pfc_gen_file = 'pfc_gen_t2.py'
        pfc_send_time = 8
    else:
        pfc_gen_file = 'pfc_gen.py'
        pfc_send_time = None
    storm_handle = PFCStorm(dut, fanout_info, fanout, pfc_queue_idx=pfc_queue_index,
                            pfc_frames_number=pfc_frames_count, pfc_gen_file=pfc_gen_file,
                            pfc_send_period=pfc_send_time, peer_info=peer_params)
    storm_handle.deploy_pfc_gen()
    return storm_handle


@pytest.mark.usefixtures('pfcwd_timer_setup_restore', 'start_background_traffic')
class TestPfcwdAllTimer(object):
    """ PFCwd timer test class """
    def run_test(self, setup_info):
        """
        Test execution
        """
        with DisableLogrotateCronContext(self.dut):
            logger.info("Flush logs")
            self.dut.shell("logrotate -f /etc/logrotate.conf")

        selected_test_ports = [setup_info['selected_test_port']]
        test_ports_info = setup_info['test_ports']
        queues = [self.storm_handle.pfc_queue_idx]

        with send_background_traffic(self.dut, self.ptf, queues, selected_test_ports, test_ports_info, pkt_count=500):
            self.storm_handle.start_storm()
            logger.info("Wait for queue to recover from PFC storm")
            time.sleep(32)
            self.storm_handle.stop_storm()
            time.sleep(16)

        if self.dut.facts['asic_type'] == 'vs':
            logger.info("Skip time detect for VS")
            return

        skip_this_loop = False
        if self.dut.topo_type == 't2' and self.storm_handle.peer_device.os == 'sonic':
            storm_detect_ms = self.retrieve_timestamp("[d]etected PFC storm")
        else:
            storm_start_ms = self.retrieve_timestamp("[P]FC_STORM_START")
            storm_detect_ms = self.retrieve_timestamp("[d]etected PFC storm")

        logger.info("Wait for PFC storm end marker to appear in logs")
        time.sleep(16)

        if self.dut.topo_type == 't2' and self.storm_handle.peer_device.os == 'sonic':
            storm_restore_ms = self.retrieve_timestamp("[s]torm restored")
        else:
            storm_end_ms = self.retrieve_timestamp("[P]FC_STORM_END")
            storm_restore_ms = self.retrieve_timestamp("[s]torm restored")

        if self.dut.topo_type == 't2' and self.storm_handle.peer_device.os == 'sonic':
            if storm_detect_ms == 0 or storm_restore_ms == 0:
                logging.warning("storm_detect_ms {} or storm_restore_ms {} is 0".format(
                    storm_detect_ms, storm_restore_ms))
                skip_this_loop = True
        else:
            if storm_start_ms == 0 or storm_detect_ms == 0 or storm_end_ms == 0 or storm_restore_ms == 0:
                logging.warning("storm_start_ms {} or storm_detect_ms {} or "
                                "storm_end_ms {} or storm_restore_ms {} is 0".format(
                                    storm_start_ms, storm_detect_ms, storm_end_ms, storm_restore_ms))
                skip_this_loop = True

        if skip_this_loop:
            logger.warning("Skip this loop due to missing timestamps")
            return

        if not (self.dut.topo_type == 't2' and self.storm_handle.peer_device.os == 'sonic'):
            real_detect_time = storm_detect_ms - storm_start_ms
            real_restore_time = storm_restore_ms - storm_end_ms
            self.all_detect_time.append(real_detect_time)
            self.all_restore_time.append(real_restore_time)

        dut_detect_restore_time = storm_restore_ms - storm_detect_ms
        logger.info(
            "Iteration all_dut_detect_time list {} and length {}".format(
                ",".join(str(i) for i in self.all_detect_time), len(self.all_detect_time)))
        self.all_dut_detect_restore_time.append(dut_detect_restore_time)
        logger.info(
            "Iteration all_dut_detect_restore_time list {} and length {}".format(
                ",".join(str(i) for i in self.all_dut_detect_restore_time), len(self.all_dut_detect_restore_time)))

    def verify_pfcwd_timers(self):
        """
        Compare the timestamps obtained and verify the timer accuracy
        """
        if self.dut.facts['asic_type'] == 'vs':
            logger.info("Skip timer verify for VS")
            return

        self.all_detect_time.sort()
        self.all_restore_time.sort()
        logger.info("Verify that real detection time is not greater than configured")
        logger.info("sorted all detect time {}".format(self.all_detect_time))
        logger.info("sorted all restore time {}".format(self.all_restore_time))

        check_point = ITERATION_NUM // 2 - 1
        config_detect_time = self.timers['pfc_wd_detect_time'] + self.timers['pfc_wd_poll_time']
        # Loose the check if two conditions are met
        # 1. Leaf-fanout is Non-Onyx or non-Mellanox SONiC devices
        # 2. Device is Mellanox plaform, Loose the check
        # 3. Device is broadcom plaform, add half of polling time as compensation for the detect config time
        # It's because the pfc_gen.py running on leaf-fanout can't guarantee the PFCWD is triggered consistently
        logger.debug("dut asic_type {}".format(self.dut.facts['asic_type']))
        for fanouthost in list(self.fanout.values()):
            if fanouthost.get_fanout_os() != "onyx" or \
                    fanouthost.get_fanout_os() == "sonic" and fanouthost.facts['asic_type'] != "mellanox":
                if self.dut.facts['asic_type'] == "mellanox":
                    logger.info("Loose the check for non-Onyx or non-Mellanox leaf-fanout testbed")
                    check_point = ITERATION_NUM // 3 - 1
                    break
                elif self.dut.facts['asic_type'] == "broadcom":
                    logger.info("Configuring detect time for broadcom DUT")
                    config_detect_time = (
                        self.timers['pfc_wd_detect_time'] +
                        self.timers['pfc_wd_poll_time'] +
                        (self.timers['pfc_wd_poll_time'] // 2)
                    )
                    break

        err_msg = ("Real detection time is greater than configured: Real detect time: {} "
                   "Expected: {} (wd_detect_time + wd_poll_time)".format(self.all_detect_time[check_point],
                                                                         config_detect_time))
        pytest_assert(self.all_detect_time[check_point] < config_detect_time, err_msg)

        if self.timers['pfc_wd_poll_time'] < self.timers['pfc_wd_detect_time']:
            logger.info("Verify that real detection time is not less than configured")
            err_msg = ("Real detection time is less than configured: Real detect time: {} "
                       "Expected: {} (wd_detect_time)".format(self.all_detect_time[check_point],
                                                              self.timers['pfc_wd_detect_time']))
            pytest_assert(self.all_detect_time[check_point] > self.timers['pfc_wd_detect_time'], err_msg)

        if self.timers['pfc_wd_poll_time'] < self.timers['pfc_wd_restore_time']:
            logger.info("Verify that real restoration time is not less than configured")
            err_msg = ("Real restoration time is less than configured: Real restore time: {} "
                       "Expected: {} (wd_restore_time)".format(self.all_restore_time[check_point],
                                                               self.timers['pfc_wd_restore_time']))
            pytest_assert(self.all_restore_time[check_point] > self.timers['pfc_wd_restore_time'], err_msg)

        logger.info("Verify that real restoration time is less than configured")
        config_restore_time = self.timers['pfc_wd_restore_time'] + self.timers['pfc_wd_poll_time']
        err_msg = ("Real restoration time is greater than configured: Real restore time: {} "
                   "Expected: {} (wd_restore_time + wd_poll_time)".format(self.all_restore_time[check_point],
                                                                          config_restore_time))
        pytest_assert(self.all_restore_time[check_point] < config_restore_time, err_msg)

    def verify_pfcwd_timers_t2(self):
        """
        Compare the timestamps obtained and verify the timer accuracy for t2 chassis
        """
        if self.dut.facts['asic_type'] == 'vs':
            logger.info("Skip timer verify for VS")
            return

        self.all_dut_detect_restore_time.sort()
        # Detect to restore elapsed time should always be less than 10 seconds since
        # storm is sent for 8 seconds
        dut_config_pfcwd_time = 10000

        logger.info(
            "all_dut_detect_restore_time sorted list {} and length {}".format(
                ",".join(str(i) for i in self.all_dut_detect_restore_time), len(self.all_dut_detect_restore_time)))

        logger.info("Verify that real dut detection-restoration time is less than expected value")
        err_msg = ("Real dut detection-restoration time is greater than configured: Real dut detection-restore time: {}"
                   " Expected: {}".format(self.all_dut_detect_restore_time[5], dut_config_pfcwd_time))
        pytest_assert(self.all_dut_detect_restore_time[5] < dut_config_pfcwd_time, err_msg)

    def retrieve_timestamp(self, pattern):
        """
        Retreives the syslog timestamp in ms associated with the pattern

        Args:
            pattern (string): pattern to be searched in the syslog

        Returns:
            timestamp_ms (int): syslog timestamp in ms for the line matching the pattern
        """
        try:
            cmd = "grep \"{}\" /var/log/syslog".format(pattern)
            syslog_msg = self.dut.shell(cmd)['stdout']

            # Regular expressions for the two timestamp formats
            regex = re.compile(r'\b[A-Za-z]{3}\s{1,2}\d{1,2} \d{2}:\d{2}:\d{2}\.\d{6}\b')
            search_string = regex.search(syslog_msg)
            if search_string:
                timestamp = search_string.group()
            else:
                logger.warning("Get timestamp: Unexpected syslog message format, syslog_msg {}".format(syslog_msg))
                return int(0)

            timestamp_ms = self.dut.shell("date -d '{}' +%s%3N".format(timestamp))['stdout']
            return int(timestamp_ms)
        except Exception as e:
            logger.warning("Get timestamp: An unexpected error occurred: pattern {} err {}".format(pattern, str(e)))
            return int(0)

    def test_pfcwd_timer_accuracy(self, duthosts, ptfhost, enum_rand_one_per_hwsku_frontend_hostname,
                                  pfcwd_timer_setup_restore, fanouthosts, set_pfc_time_cisco_8000):
        """
        Tests PFCwd timer accuracy

        Args:
            duthost (AnsibleHost): DUT instance
            pfcwd_timer_setup_restore (fixture): class scoped autouse setup fixture
        """
        duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
        setup_info = pfcwd_timer_setup_restore
        self.storm_handle = setup_info['storm_handle']
        self.timers = setup_info['timers']
        self.dut = duthost
        self.ptf = ptfhost
        self.fanout = fanouthosts
        self.all_detect_time = list()
        self.all_restore_time = list()
        self.all_dut_detect_restore_time = list()
        try:
            if self.dut.topo_type == 't2' and self.storm_handle.peer_device.os == 'sonic':
                for i in range(1, 11):
                    logger.info("--- Pfcwd Timer Test iteration #{}".format(i))
                    self.run_test(setup_info)
                self.verify_pfcwd_timers_t2()
            else:
                for i in range(1, ITERATION_NUM):
                    logger.info("--- Pfcwd Timer Test iteration #{}".format(i))

                    cmd = "show pfc counters"
                    pfcwd_cmd_response = self.dut.shell(cmd, module_ignore_errors=True)
                    logger.debug("loop {} cmd {} rsp {}".format(i, cmd, pfcwd_cmd_response.get('stdout', None)))

                    cmd = "show pfcwd stats"
                    pfcwd_cmd_response = self.dut.shell(cmd, module_ignore_errors=True)
                    logger.debug("loop {} cmd {} rsp {}".format(i, cmd, pfcwd_cmd_response.get('stdout', None)))

                    self.run_test(setup_info)
                self.verify_pfcwd_timers()

        except Exception as e:
            logger.info("exception: ")
            cmd = "show pfc counters"
            pfcwd_cmd_response = self.dut.shell(cmd, module_ignore_errors=True)
            logger.info("pfcwd_cmd {} response: {}".format(cmd, pfcwd_cmd_response.get('stdout', None)))

            cmd = "show pfcwd stats"
            pfcwd_cmd_response = self.dut.shell(cmd, module_ignore_errors=True)
            logger.info("pfcwd_cmd {} response: {}".format(cmd, pfcwd_cmd_response.get('stdout', None)))

            pytest.fail(str(e))

        finally:
            if self.storm_handle:
                self.storm_handle.stop_storm()
