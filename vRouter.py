#!/usr/bin/python
# coding: utf8
#######################################################################
#
# Copyright (c) 2016 Okinawa Open Laboratory
#
# All rights reserved. This program and the accompanying materials
# are made available under the terms of the Apache License, Version 2.0
# which accompanies this distribution, and is available at
# http://www.apache.org/licenses/LICENSE-2.0
########################################################################

import argparse
import datetime
import os
import pprint
import time
import yaml


from git import Repo

import glanceclient.client as glclient
import keystoneclient.v2_0.client as ksclient
import novaclient.client as nvclient
from neutronclient.v2_0 import client as ntclient


import functest.utils.functest_utils as functest_utils
import functest.utils.openstack_utils as os_utils

from test_controller.test_exec import Test_exec
from test_controller.performance_test_exec import Performance_test_exec
from orchestrator import orchestrator
from topology import topology
from utilvnf import utilvnf

pp = pprint.PrettyPrinter(indent=4)


parser = argparse.ArgumentParser()
parser.add_argument("-d",
                    "--debug",
                    help="Debug mode",
                    action="store_true")
parser.add_argument("-r",
                    "--report",
                    help="Create json result file",
                    action="store_true")
parser.add_argument("-n",
                    "--noclean",
                    help="Don't clean the created resources for this test.",
                    action="store_true")

OPNFV_VNF_DATA_DIR = "opnfv-vnf-data/"
TEST_SCENATIO_YAML = "test_scenario.yaml"
TEST_ENV_CONFIG_YAML = "test_env_config.yaml"

with open(os.environ["CONFIG_FUNCTEST_YAML"]) as f:
    functest_yaml = yaml.safe_load(f)
f.close()

# Cloudify parameters
VNF_DIR = functest_yaml.get("general").get("directories").get(
          "dir_repo_vRouter") + "/"
VNF_DATA_DIR = functest_yaml.get("general").get(
    "directories").get("dir_vRouter_data") + "/"
DB_URL = functest_yaml.get("results").get("test_db_url")

TENANT_NAME = functest_yaml.get("vRouter").get("general").get("tenant_name")
TENANT_DESCRIPTION = functest_yaml.get("vRouter").get(
    "general").get("tenant_description")
IMAGES = functest_yaml.get("vRouter").get("general").get("images")
TEST_DATA = functest_yaml.get("vRouter").get("general").get("test_data")

CFY_MANAGER_BLUEPRINT = functest_yaml.get(
    "vRouter").get("cloudify").get("blueprint")
CFY_MANAGER_REQUIERMENTS = functest_yaml.get(
    "vRouter").get("cloudify").get("requierments")
CFY_INPUTS = functest_yaml.get("vRouter").get("cloudify").get("inputs")


TEST_ENV_CONFIG_YAML_FILE_PATH = VNF_DATA_DIR + \
                                 OPNFV_VNF_DATA_DIR + \
                                 TEST_ENV_CONFIG_YAML
with open(TEST_ENV_CONFIG_YAML_FILE_PATH) as f:
    test_env_config_yaml = yaml.safe_load(f)
f.close()

VNF_TEST_IMAGES = test_env_config_yaml.get("general").get("images")
IMAGES.update(VNF_TEST_IMAGES)

TEST_SCENATIO_YAML_FILE_PATH = VNF_DATA_DIR + \
                               OPNFV_VNF_DATA_DIR + \
                               TEST_SCENATIO_YAML

FUNCTION_TEST_TPLGY_BLUEPRINT = test_env_config_yaml.get("test_topology").get(
    "function_test_topology").get("blueprint")

FUNCTION_TEST_TPLGY_BP_NAME = test_env_config_yaml.get("test_topology").get(
    "function_test_topology").get("blueprint").get("blueprint_name")

FUNCTION_TEST_TPLGY_DEPLOY_NAME = test_env_config_yaml.get("test_topology").get(
    "function_test_topology").get("blueprint").get("deployment_name")

FUNCTION_TEST_TPLGY_DEFAULT = test_env_config_yaml.get("test_topology").get(
    "function_test_topology").get("default")

PERFORMANCE_TPLGY_BLUEPRINT = test_env_config_yaml.get("test_topology").get(
    "performance_test_topology").get("blueprint")

PERFORMANCE_TPLGY_BP_NAME = test_env_config_yaml.get("test_topology").get(
    "performance_test_topology").get("blueprint").get(
    "blueprint_name")

PERFORMANCE_TPLGY_DEPLOY_NAME = test_env_config_yaml.get("test_topology").get(
    "performance_test_topology").get("blueprint").get(
    "deployment_name")

PERFORMANCE_TEST_TPLGY_DEFAULT = test_env_config_yaml.get("test_topology").get(
    "performance_test_topology").get("default")

REBOOT_WAIT = test_env_config_yaml.get("general").get(
    "reboot_wait")


class vRouter:
    def __init__(self, logger):

        """ logging configuration """
        self.logger = logger

        REPO_PATH = os.environ['repos_dir'] + '/functest/'
        if not os.path.exists(REPO_PATH):
            self.logger.error("Repos directory not found '%s'" % REPO_PATH)
            exit(-1)

        self.testcase_start_time = time.time()

        self.results = {
            'init': {
                'duration': 0,
                'result': 'none'
            },
            'making_orchestrator': {
                'duration': 0,
                'result': 'none'
            },
            'making_testTopology': {
                'duration': 0,
                'result': 'none'
            },
            'testing_vRouter': {
                'duration': 0,
                'result': 'none'
            }
        }

        self.ks_cresds = None
        self.nv_cresds = None
        self.nt_cresds = None
        self.glance = None
        self.neutron = None

    def download_and_add_image_on_glance(self, glance, image_name, image_url):
        dest_path = VNF_DATA_DIR + "tmp/"
        if not os.path.exists(dest_path):
            os.makedirs(dest_path)
        file_name = image_url.rsplit('/')[-1]

        result = functest_utils.download_url(image_url,
                                             dest_path)
        if not result:
            self.logger.error("Failed to download image %s" % file_name)
            return False

        image = os_utils.create_glance_image(glance,
                                             image_name,
                                             dest_path + file_name)
        if not image:
            self.logger.error("Failed to upload image on glance")
            return False

        return image

    def set_result(self, step_name, duration=0, result=""):
        self.results[step_name] = {
            'duration': duration,
            'result': result
        }

    def set_resultdata(self, start_time, stop_time, status, results):
        result_data = {}
        result_data["start_time"] = start_time
        result_data["stop_time"] = stop_time
        result_data["status"] = status
        result_data["results"] = results

        if status == "PASS":
            self.logger.info(" result_data %s", result_data)

        return result_data

    def step_failure(self, step_name, error_msg):
        stop_time = time.time()
        self.logger.error(error_msg)
        self.set_result(step_name,
                        0,
                        error_msg)
        status = "FAIL"
        # in case of failure starting and stoping time are not correct
        result_data = self.set_resultdata(self.testcase_start_time, stop_time,
                                          status, self.results)
        return result_data

    def init_vRouter_test(self, cfy):
        self.util_info = {"credentials": self.credentials,
                          "cfy": cfy,
                          "vnf_data_dir": VNF_DATA_DIR}

        self.cfy_manager_ip = self.util.get_cfy_manager_address(cfy,
                                                                VNF_DATA_DIR)

        self.logger.debug("cfy manager address : %s" % self.cfy_manager_ip)

    def function_test_vRouter(self, cfy, target_vnf_name, test_info):
        test_protocol = test_info["protocol"]
        test_list = test_info[test_protocol]

        vnf_info_list = self.util.get_vnf_info_list(self.cfy_manager_ip,
                                                    FUNCTION_TEST_TPLGY_DEPLOY_NAME,
                                                    target_vnf_name)

        self.logger.debug("request vnf's reboot.")
        self.util.request_vnf_reboot(vnf_info_list)
        time.sleep(REBOOT_WAIT)

        target_vnf = self.util.get_target_vnf(vnf_info_list)
        if target_vnf is None:
            return self.step_failure(
                "testing_vRouter",
                "Error : target_vnf is None.")

        reference_vnf_list = self.util.get_reference_vnf_list(vnf_info_list)
        if len(reference_vnf_list) == 0:
            return self.step_failure(
                "testing_vRouter",
                "Error : reference_vnf_list is empty.")

        test_exec = Test_exec(self.util_info)

        # start test
        start_time_ts = time.time()
        self.logger.info("vRouter test Start Time:'%s'" % (
            datetime.datetime.fromtimestamp(start_time_ts).strftime(
                '%Y-%m-%d %H:%M:%S')))

        result = test_exec.run(target_vnf,
                               reference_vnf_list,
                               test_protocol,
                               test_list)
        result = True

        end_time_ts = time.time()
        duration = round(end_time_ts - start_time_ts,
                         1)
        self.logger.info("vRouter test duration :'%s'" % duration)

        self.end_time_ts = end_time_ts

        if result:
            self.set_result("testing_vRouter",
                            duration,
                            "OK")

        self.vnf_info_list = vnf_info_list

        return result

    def performance_test_vRouter(self, cfy, performance_test_scenario,
                                 performance_test_info):
        input_parameter = performance_test_info["input_parameter"]

        vnf_info_list = self.util.get_vnf_info_list_for_performance_test(
                                      self.cfy_manager_ip,
                                      PERFORMANCE_TPLGY_DEPLOY_NAME,
                                      performance_test_scenario)

        target_vnf = self.util.get_target_vnf(vnf_info_list)
        if target_vnf is None:
            return self.step_failure(
                "testing_vRouter",
                "Error : target_vnf is None.")

        send_tester_vm = self.util.get_send_tester_vm(vnf_info_list)
        if send_tester_vm is None:
            return self.step_failure(
                "testing_vRouter",
                "Error : send_tester_vm is None.")

        receive_tester_vm = self.util.get_receive_tester_vm(vnf_info_list)
        if receive_tester_vm is None:
            return self.step_failure(
                "testing_vRouter",
                "Error : receive_tester_vm is None.")

        test_exec = Performance_test_exec(self.util_info)

        # start test
        start_time_ts = time.time()
        self.logger.info("vRouter performance test Start Time:'%s'" % (
            datetime.datetime.fromtimestamp(start_time_ts).strftime(
                '%Y-%m-%d %H:%M:%S')))

        result = test_exec.run(target_vnf,
                               send_tester_vm,
                               receive_tester_vm,
                               input_parameter)
        result = True

        end_time_ts = time.time()
        duration = round(end_time_ts - start_time_ts,
                         1)
        self.logger.info("vRouter test duration :'%s'" % duration)

        self.end_time_ts = end_time_ts

        if result:
            self.set_result("testing_vRouter",
                            duration,
                            "OK")

        self.vnf_info_list = vnf_info_list

        return result

    def test_vRouter(self, cfy):
        result = False

        test_scenario_list = self.test_scenario_yaml["test_scenario_list"]

        for test_scenario in test_scenario_list:
            if test_scenario["test_type"] == "function_test":
                function_test_scenario = test_scenario

                # FUNCTION TEST TOPOLOGY INITIALISATION
                function_tplgy = topology(orchestrator=cfy,
                                          logger=self.logger)

                result_data = self.init_function_testToplogy(function_tplgy,
                                                             function_test_scenario)
                if result_data["status"] == "FAIL":
                    return result_data

                # FUNCTION TEST TOPOLOGY DEPLOYMENT
                blueprint_info = \
                    {"url": FUNCTION_TEST_TPLGY_BLUEPRINT,
                     "blueprint_name": FUNCTION_TEST_TPLGY_BP_NAME,
                     "deployment_name": FUNCTION_TEST_TPLGY_DEPLOY_NAME}

                result_data = self.deploy_testToplogy(function_tplgy,
                                                      blueprint_info)
                if result_data["status"] == "FAIL":
                    return result_data

                time.sleep(120)

                # FUNCTION TEST EXECUTION
                function_test_list = function_test_scenario["function_test_list"]
                for function_test in function_test_list:
                    test_list = function_test["test_list"]
                    target_vnf_name = function_test["target_vnf_name"]
                    for test_info in test_list:
                        self.logger.info(test_info["protocol"] + " " +
                                         test_info["test_kind"] +
                                         " test.")
                        result = self.function_test_vRouter(cfy,
                                                            target_vnf_name,
                                                            test_info)
                        if not result:
                            break

                self.logger.debug("request vnf's delete.")
                self.util.request_vm_delete(self.vnf_info_list)

                # FUNCTION TEST TOPOLOGY UNDEPLOYMENT
                function_tplgy.undeploy_vnf(FUNCTION_TEST_TPLGY_DEPLOY_NAME)

            elif test_scenario["test_type"] == "performance_test":
                performance_test_scenario = test_scenario

                # PERFORMANCE_ TEST TOPOLOGY INITIALISATION
                performance_tplgy = topology(orchestrator=cfy,
                                             logger=self.logger)

                result_data = self.init_performance_testToplogy(
                                       performance_tplgy,
                                       performance_test_scenario)
                if result_data["status"] == "FAIL":
                    return result_data

                # PERFORMANCE TEST TOPOLOGY DEPLOYMENT
                blueprint_info = \
                    {"url": PERFORMANCE_TPLGY_BLUEPRINT,
                     "blueprint_name": PERFORMANCE_TPLGY_BP_NAME,
                     "deployment_name": PERFORMANCE_TPLGY_DEPLOY_NAME}

                result_data = self.deploy_testToplogy(performance_tplgy,
                                                      blueprint_info)
                if result_data["status"] == "FAIL":
                    return result_data

                # PERFORMANCE TEST EXECUTION
                performance_test_list = performance_test_scenario["performance_test_list"]
                for performance_test_info in performance_test_list:
                    result = self.performance_test_vRouter(
                                      cfy,
                                      performance_test_scenario,
                                      performance_test_info)

                self.logger.debug("request vnf's delete.")
                self.util.request_vm_delete(self.vnf_info_list)

                # PERFORMANCE TEST TOPOLOGY UNDEPLOYMENT
                performance_tplgy.undeploy_vnf(PERFORMANCE_TPLGY_DEPLOY_NAME)

            else:
                return self.step_failure(
                    "testing_vRouter",
                    "Error : Unknown topology type.")

        if result:
            return self.set_resultdata(self.testcase_start_time,
                                       self.end_time_ts,
                                       "PASS", self.results)

        return self.step_failure(
            "testing_vRouter",
            "Error : Faild to test execution.")

    def init(self):

        start_time_ts = time.time()

        if not os.path.exists(VNF_DATA_DIR):
            os.makedirs(VNF_DATA_DIR)

        self.ks_cresds = os_utils.get_credentials("keystone")
        self.nv_cresds = os_utils.get_credentials("nova")
        self.nt_cresds = os_utils.get_credentials("neutron")

        self.logger.info("Prepare OpenStack plateform(create tenant and user)")
        keystone = ksclient.Client(**self.ks_cresds)

        user_id = os_utils.get_user_id(keystone,
                                       self.ks_cresds['username'])
        if user_id == '':
            return self.step_failure("init",
                                     "Error : Failed to get id of " +
                                     self.ks_cresds['username'])

        tenant_id = os_utils.create_tenant(keystone,
                                           TENANT_NAME,
                                           TENANT_DESCRIPTION)
        if tenant_id == '':
            return self.step_failure("init",
                                     "Error : Failed to create " +
                                     TENANT_NAME + " tenant")
        roles_name = [
            "admin",
            "Admin"
        ]
        role_id = ''
        for role_name in roles_name:
            if role_id == '':
                role_id = os_utils.get_role_id(keystone,
                                               role_name)

        if role_id == '':
            self.logger.error("Error : Failed to get id for %s role" %
                              role_name)

        if not os_utils.add_role_user(keystone,
                                      user_id,
                                      role_id,
                                      tenant_id):

            self.logger.error("Error : Failed to add %s on tenant" %
                              self.ks_cresds['username'])

        user_id = os_utils.create_user(keystone,
                                       TENANT_NAME,
                                       TENANT_NAME,
                                       None,
                                       tenant_id)
        if user_id == '':
            self.logger.error("Error : Failed to create %s user" % TENANT_NAME)

        self.logger.info("Update OpenStack creds informations")
        self.ks_cresds.update({
            "username": TENANT_NAME,
            "password": TENANT_NAME,
            "tenant_name": TENANT_NAME,
        })

        self.nt_cresds.update({
            "tenant_name": TENANT_NAME,
        })

        self.nv_cresds.update({
            "project_id": TENANT_NAME,
        })

        self.logger.info("Upload some OS images if it doesn't exist")
        glance_endpoint = keystone.service_catalog.url_for(
                                            service_type='image',
                                            endpoint_type='publicURL')

        self.glance = glclient.Client(1,
                                      glance_endpoint,
                                      token=keystone.auth_token)

        images = {}
        images.update(IMAGES)
        images.update(VNF_TEST_IMAGES)
        for img in images.keys():
            image_name = images[img]['image_name']
            self.logger.info("image name = " + image_name)
            image_url = images[img]['image_url']

            image_id = os_utils.get_image_id(self.glance,
                                             image_name)

            if image_id == '':
                self.logger.info("""%s image doesn't exist on glance repository. Try
                downloading this image and upload on glance !""" % image_name)
                image_id = self.download_and_add_image_on_glance(self.glance,
                                                                 image_name,
                                                                 image_url)

            if image_id == '':
                return self.step_failure(
                    "init",
                    "Error : Failed to find or upload required OS "
                    "image for this deployment")

        self.logger.info("Update security group quota for this tenant")
        self.neutron = ntclient.Client(**self.nt_cresds)

        result = os_utils.update_sg_quota(self.neutron,
                                          tenant_id,
                                          50,
                                          100)

        if not result:
            return self.step_failure(
                "init",
                "Failed to update security group quota for tenant " +
                TENANT_NAME)

        self.credentials = {"username": TENANT_NAME,
                            "password": TENANT_NAME,
                            "auth_url": os.environ['OS_AUTH_URL'],
                            "tenant_name": TENANT_NAME,
                            "region_name": os.environ['OS_REGION_NAME']}

        self.util = utilvnf(self.logger)
        self.util.set_credentials(self.credentials["username"],
                                  self.credentials["password"],
                                  self.credentials["auth_url"],
                                  self.credentials["tenant_name"],
                                  self.credentials["region_name"])

        self.logger.debug("Downloading the test data.")
        vRouter_data_path = VNF_DATA_DIR + OPNFV_VNF_DATA_DIR

        if not os.path.exists(vRouter_data_path):
            Repo.clone_from(TEST_DATA['url'],
                            vRouter_data_path,
                            branch=TEST_DATA['branch'])

        test_scenario_file = open(TEST_SCENATIO_YAML_FILE_PATH,
                                'r')
        self.test_scenario_yaml = yaml.safe_load(test_scenario_file)
        test_scenario_file.close()

        res = self.util.test_scenario_validation_check(self.test_scenario_yaml)
        if res["status"] is False:
            self.logger.error(res["message"])
            return self.step_failure(
                           "init",
                           "Error : Faild to test scenario format")

        end_time_ts = time.time()
        duration = round(end_time_ts - start_time_ts,
                         1)

        self.set_result("init",
                        duration,
                        "OK")

        return self.set_resultdata(self.testcase_start_time, "",
                                   "", self.results)

    def deploy_cloudify(self, cfy):

        username = self.ks_cresds['username']
        password = self.ks_cresds['password']
        tenant_name = self.ks_cresds['tenant_name']
        auth_url = self.ks_cresds['auth_url']

        cfy.set_credentials(username,
                            password,
                            tenant_name,
                            auth_url)

        self.logger.info("Collect flavor id for cloudify manager server")

        nova = nvclient.Client("2",
                               **self.nv_cresds)

        flavor_name = "m1.large"
        flavor_id = os_utils.get_flavor_id(nova,
                                           flavor_name)

        for requirement in CFY_MANAGER_REQUIERMENTS:
            if requirement == 'ram_min':
                flavor_id = os_utils.get_flavor_id_by_ram_range(
                                nova,
                                CFY_MANAGER_REQUIERMENTS['ram_min'],
                                320000)

        if flavor_id == '':
            self.logger.error(
                "Failed to find %s flavor. "
                "Try with ram range default requirement !" % flavor_name)
            flavor_id = os_utils.get_flavor_id_by_ram_range(nova,
                                                            4000,
                                                            8196)

        if flavor_id == '':
            return self.step_failure(
                        "making_orchestrator",
                        "Failed to find required flavor for this deployment")

        cfy.set_flavor_id(flavor_id)

        image_name = "centos_7"
        image_id = os_utils.get_image_id(self.glance,
                                         image_name)

        for requirement in CFY_MANAGER_REQUIERMENTS:
            if requirement == 'os_image':
                image_id = os_utils.get_image_id(
                               self.glance,
                               CFY_MANAGER_REQUIERMENTS['os_image'])

        if image_id == '':
            return self.step_failure(
              "making_orchestrator",
              "Error : Failed to find required OS image for cloudify manager")

        cfy.set_image_id(image_id)

        ext_net = os_utils.get_external_net(self.neutron)
        if not ext_net:
            return self.step_failure(
                         "making_orchestrator",
                         "Failed to get external network")

        cfy.set_external_network_name(ext_net)

        ns = functest_utils.get_resolvconf_ns()
        if ns:
            cfy.set_nameservers(ns)

        self.logger.info("Prepare virtualenv for cloudify-cli")
        cmd = "chmod +x " + VNF_DIR + "create_venv.sh"
        functest_utils.execute_command(cmd,
                                       self.logger)
        time.sleep(3)
        cmd = VNF_DIR + "create_venv.sh " + VNF_DATA_DIR
        functest_utils.execute_command(cmd,
                                       self.logger)

        cfy.download_manager_blueprint(
            CFY_MANAGER_BLUEPRINT['url'],
            CFY_MANAGER_BLUEPRINT['branch'])

        # ############### CLOUDIFY DEPLOYMENT ################
        start_time_ts = time.time()
        self.logger.info("Cloudify deployment Start Time:'%s'" % (
            datetime.datetime.fromtimestamp(start_time_ts).strftime(
                '%Y-%m-%d %H:%M:%S')))

        error = cfy.deploy_manager()
        if error:
            return self.step_failure("making_orchestrator",
                                     error)

        end_time_ts = time.time()
        duration = round(end_time_ts - start_time_ts,
                         1)
        self.logger.info("Cloudify deployment duration:'%s'" % duration)

        self.set_result("making_orchestrator",
                        duration,
                        "OK")

        return self.set_resultdata(self.testcase_start_time, "",
                                   "", self.results)

    def init_function_testToplogy(self, tplgy, function_test_config):
        self.logger.info("Collect flavor id for all topology vnf")
        nova = nvclient.Client("2",
                               **self.nv_cresds)

        vnf_list = function_test_config["vnf_list"]
        target_vnf = self.util.get_vnf_info(vnf_list, "target_vnf")
        reference_vnf = self.util.get_vnf_info(vnf_list, "reference_vnf")

        target_vnf_image_name = ""
        if "image_name" in target_vnf:
            target_vnf_image_name = target_vnf["image_name"]
        target_vnf_flavor_name = ""
        if "flavor_name" in target_vnf:
            target_vnf_flavor_name = target_vnf["flavor_name"]
        self.logger.debug("target_vnf image name : " + target_vnf_image_name)
        self.logger.debug("target_vnf flavor name : " + target_vnf_flavor_name)

        reference_vnf_image_name = ""
        if "image_name" in reference_vnf:
            reference_vnf_image_name = reference_vnf["image_name"]
        reference_vnf_flavor_name = ""
        if "flavor_name" in reference_vnf:
            reference_vnf_flavor_name = reference_vnf["flavor_name"]
        self.logger.debug("reference_vnf image name : " + reference_vnf_image_name)
        self.logger.debug("reference_vnf flavor name : " + reference_vnf_flavor_name)

        nova = nvclient.Client("2",
                               **self.nv_cresds)

        # Setting the flavor id for target vnf.
        target_vnf_flavor_id = os_utils.get_flavor_id(
                                            nova,
                                            target_vnf_flavor_name)

        if target_vnf_flavor_id == '':
            for default in FUNCTION_TEST_TPLGY_DEFAULT:
                if default == 'ram_min':
                    target_vnf_flavor_id = os_utils.get_flavor_id_by_ram_range(
                        nova,
                        FUNCTION_TEST_TPLGY_DEFAULT['ram_min'],
                        8196)

            self.logger.info("target_vnf_flavor_id id search set")

        if target_vnf_flavor_id == '':
            return self.step_failure(
                "making_testTopology",
                "Error : Failed to find flavor for target vnf")

        tplgy.set_target_vnf_flavor_id(target_vnf_flavor_id)

        # Setting the flavor id for reference vnf.
        reference_vnf_flavor_id = os_utils.get_flavor_id(
            nova,
            reference_vnf_flavor_name)

        if reference_vnf_flavor_id == '':
            for default in FUNCTION_TEST_TPLGY_DEFAULT:
                if default == 'ram_min':
                    reference_vnf_flavor_id = \
                        os_utils.get_flavor_id_by_ram_range(
                            nova,
                            FUNCTION_TEST_TPLGY_DEFAULT['ram_min'],
                            8196)

            self.logger.info("reference_vnf_flavor_id id search set")

        if reference_vnf_flavor_id == '':
            return self.step_failure(
                "making_testTopology",
                "Error : Failed to find flavor for tester vm")

        tplgy.set_reference_vnf_flavor_id(reference_vnf_flavor_id)

        # Setting the image id for target vnf.
        target_vnf_image_id = os_utils.get_image_id(
            self.glance,
            target_vnf_image_name)

        if target_vnf_image_id == '':
            for default in FUNCTION_TEST_TPLGY_DEFAULT:
                if default == 'os_image':
                    target_vnf_image_id = os_utils.get_image_id(
                        self.glance,
                        FUNCTION_TEST_TPLGY_DEFAULT['os_image'])

        if target_vnf_image_id == '':
            return self.step_failure(
               "making_testTopology",
               "Error : Failed to find required OS image for target vnf")

        tplgy.set_target_vnf_image_id(target_vnf_image_id)

        # Setting the image id for reference vnf.
        reference_vnf_image_id = os_utils.get_image_id(
            self.glance,
            reference_vnf_image_name)

        if reference_vnf_image_id == '':
            for default in FUNCTION_TEST_TPLGY_DEFAULT:
                if default == 'os_image':
                    reference_vnf_image_id = os_utils.get_image_id(
                        self.glance,
                        FUNCTION_TEST_TPLGY_DEFAULT['os_image'])

        if reference_vnf_image_id == '':
            return self.step_failure(
               "making_testTopology",
               "Error : Failed to find required OS image for reference vnf.")

        tplgy.set_reference_vnf_image_id(reference_vnf_image_id)

        tplgy.set_region("RegionOne")

        ext_net = os_utils.get_external_net(self.neutron)
        if not ext_net:
            return self.step_failure(
                   "making_testTopology",
                   "Failed to get external network")

        tplgy.set_external_network_name(ext_net)

        tplgy.set_credentials(username=self.ks_cresds['username'],
                              password=self.ks_cresds['password'],
                              tenant_name=self.ks_cresds['tenant_name'],
                              auth_url=self.ks_cresds['auth_url'])

        return self.set_resultdata(self.testcase_start_time, "",
                                   "", self.results)

    def init_performance_testToplogy(self, tplgy, performance_test_config):

        vnf_list = performance_test_config["vnf_list"]
        target_vnf = self.util.get_vnf_info(vnf_list, "target_vnf")
        tester_vm = self.util.get_vnf_info(vnf_list, "tester_vm")

        target_vnf_image_name = ""
        if "image_name" in target_vnf:
            target_vnf_image_name = target_vnf["image_name"]
        target_vnf_flavor_name = ""
        if "flavor_name" in target_vnf:
            target_vnf_flavor_name = target_vnf["flavor_name"]
        self.logger.debug("target_vnf image name : " + target_vnf_image_name)
        self.logger.debug("target_vnf flavor name : " + target_vnf_flavor_name)

        tester_vm_image_name = ""
        if "image_name" in tester_vm:
            tester_vm_image_name = tester_vm["image_name"]
        tester_vm_flavor_name = ""
        if "flavor_name" in tester_vm:
            tester_vm_flavor_name = tester_vm["flavor_name"]
        self.logger.debug("tester vm image name : " + tester_vm_image_name)
        self.logger.debug("tester vm flavor name : " + tester_vm_flavor_name)

        nova = nvclient.Client("2",
                               **self.nv_cresds)

        # Setting the flavor id for target vnf.
        target_vnf_flavor_id = os_utils.get_flavor_id(
            nova,
            target_vnf_flavor_name)

        if target_vnf_flavor_id == '':
            for default in PERFORMANCE_TEST_TPLGY_DEFAULT:
                if default == 'ram_min':
                    target_vnf_flavor_id = os_utils.get_flavor_id_by_ram_range(
                        nova,
                        PERFORMANCE_TEST_TPLGY_DEFAULT['ram_min'],
                        8196)

        if target_vnf_flavor_id == '':
            return self.step_failure(
                "making_testTopology",
                "Error : Failed to find flavor for target vnf")

        tplgy.set_target_vnf_flavor_id(target_vnf_flavor_id)

        # Setting the flavor id for tester vm.
        tester_vm_flavor_id = os_utils.get_flavor_id(
            nova,
            tester_vm_flavor_name)

        if tester_vm_flavor_id == '':
            for default in PERFORMANCE_TEST_TPLGY_DEFAULT:
                if default == 'ram_min':
                    tester_vm_flavor_id = os_utils.get_flavor_id_by_ram_range(
                        nova,
                        PERFORMANCE_TEST_TPLGY_DEFAULT['ram_min'],
                        8196)

        if tester_vm_flavor_id == '':
            return self.step_failure(
                "making_testTopology",
                "Error : Failed to find flavor for tester vm")

        tplgy.set_send_tester_vm_flavor_id(tester_vm_flavor_id)
        tplgy.set_receive_tester_vm_flavor_id(tester_vm_flavor_id)

        # Setting the image id for target vnf.
        target_vnf_image_id = os_utils.get_image_id(
            self.glance,
            target_vnf_image_name)

        if target_vnf_image_id == '':
            for default in PERFORMANCE_TEST_TPLGY_DEFAULT:
                if default == 'vnf_os_image':
                    target_vnf_image_id = os_utils.get_image_id(
                        self.glance,
                        PERFORMANCE_TEST_TPLGY_DEFAULT['vnf_os_image'])

        if target_vnf_image_id == '':
            return self.step_failure(
               "making_testTopology",
               "Error : Failed to find required OS image for target vnf")

        tplgy.set_target_vnf_image_id(target_vnf_image_id)

        # Setting the image id for target vnf.
        tester_vm_image_id = os_utils.get_image_id(
            self.glance,
            tester_vm_image_name)

        if tester_vm_image_id == '':
            for default in PERFORMANCE_TEST_TPLGY_DEFAULT:
                if default == 'tester_os_image':
                    tester_vm_image_id = os_utils.get_image_id(
                        self.glance,
                        PERFORMANCE_TEST_TPLGY_DEFAULT['tester_os_image'])

        if tester_vm_image_id == '':
            return self.step_failure(
               "making_testTopology",
               "Error : Failed to find required OS image for tester vm")

        tplgy.set_send_tester_vm_image_id(tester_vm_image_id)
        tplgy.set_receive_tester_vm_image_id(tester_vm_image_id)

        tplgy.set_region("RegionOne")

        ext_net = os_utils.get_external_net(self.neutron)
        if not ext_net:
            return self.step_failure(
                   "making_testTopology",
                   "Failed to get external network")

        tplgy.set_external_network_name(ext_net)

        tplgy.set_credentials(username=self.ks_cresds['username'],
                              password=self.ks_cresds['password'],
                              tenant_name=self.ks_cresds['tenant_name'],
                              auth_url=self.ks_cresds['auth_url'])

        return self.set_resultdata(self.testcase_start_time, "",
                                   "", self.results)

    def deploy_testToplogy(self, tplgy, blueprint_info):

        start_time_ts = time.time()
        end_time_ts = start_time_ts
        self.logger.info("vRouter VNF deployment Start Time:'%s'" % (
            datetime.datetime.fromtimestamp(start_time_ts).strftime(
                '%Y-%m-%d %H:%M:%S')))

        # deploy
        ret = tplgy.deploy_vnf(blueprint_info["url"],
                               blueprint_info["blueprint_name"],
                               blueprint_info["deployment_name"])
        if ret:
            self.logger.error("Error :deployment testtopology :%s", ret)
            return self.step_failure("making_testTopology",
                                     "Failed to deploy test topology")

        end_time_ts = time.time()
        duration = round(end_time_ts - start_time_ts,
                         1)
        self.logger.info("vRouter VNF deployment duration:'%s'" % duration)
        self.set_result("making_testTopology",
                        duration,
                        "OK")

        return self.set_resultdata(self.testcase_start_time, "",
                                   "", self.results)

    def clean_enviroment(self, cfy):

        # ########### CLOUDIFY UNDEPLOYMENT #############

        cfy.undeploy_manager()

        # ############## TNENANT CLEANUP ################

        self.ks_cresds = os_utils.get_credentials("keystone")

        self.logger.info("Removing %s tenant .." %
                         CFY_INPUTS['keystone_tenant_name'])

        keystone = ksclient.Client(**self.ks_cresds)
        tenant_id = os_utils.get_tenant_id(keystone,
                                           CFY_INPUTS['keystone_tenant_name'])
        if tenant_id == '':
            self.logger.error(
                         "Error : Failed to get id of %s tenant" %
                         CFY_INPUTS['keystone_tenant_name'])
        else:
            resulut = os_utils.delete_tenant(keystone,
                                             tenant_id)
            if not resulut:
                self.logger.error(
                       "Error : Failed to remove %s tenant" %
                       CFY_INPUTS['keystone_tenant_name'])

        self.logger.info("Removing %s user .." %
                         CFY_INPUTS['keystone_username'])

        user_id = os_utils.get_user_id(keystone,
                                       CFY_INPUTS['keystone_username'])

        if user_id == '':
            self.logger.error("Error : Failed to get id of %s user" %
                              CFY_INPUTS['keystone_username'])
        else:
            result = os_utils.delete_user(keystone,
                                          user_id)
            if not result:
                self.logger.error("Error : Failed to remove %s user" %
                                  CFY_INPUTS['keystone_username'])

        return self.set_resultdata(self.testcase_start_time, "",
                                   "", self.results)

    def main(self):

        # ############### GENERAL INITIALISATION ################

        result_data = self.init()

        if result_data["status"] == "FAIL":
            return result_data

        # ############### CLOUDIFY DEPLOYMENT ################

        cfy = orchestrator(VNF_DATA_DIR,
                           CFY_INPUTS,
                           self.logger)

        result_data = self.deploy_cloudify(cfy)
        if result_data["status"] == "FAIL":
            return result_data

        # ############### VNF TEST ################

        result_data = self.init_vRouter_test(cfy)

        result_data = self.test_vRouter(cfy)

        # ############### CLEAN ENVIROMENT ################
        self.clean_enviroment(cfy)

        return result_data
