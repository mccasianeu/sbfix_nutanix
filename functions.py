#!/usr/bin/env python3
from typing import Any

import argparse
import getpass
import logging
import ssl
import warnings
import sys
import time
import urllib3

from datetime import datetime

from ntnx_vmm_py_client import (
    ApiClient as VmApiClient,
    Configuration as VmConfiguration,
)
from ntnx_vmm_py_client.api.vm_api import VmApi

from ntnx_prism_py_client import (
    ApiClient as CategoriesApiClient,
    ApiClient as TasksApiClient,
    Configuration as CategoriesConfiguration,
    Configuration as TasksConfiguration,
)
from ntnx_prism_py_client.api.categories_api import CategoriesApi
from ntnx_prism_py_client.api.tasks_api import TasksApi
from ntnx_vmm_py_client import AhvConfigAssociateVmCategoriesParams


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List VMs tagged with category sbfix_needed:true from Prism Central, "
            "show their boot/security state, and prepare an SBfix run."
        )
    )
    parser.add_argument("--pc", required=True, help="Prism Central FQDN or IP address")
    parser.add_argument("--username", required=True, help="Prism Central username")
    parser.add_argument(
        "--password",
        help="Prism Central password. If omitted, an interactive prompt is used.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9440,
        help="Prism Central API port. Default: 9440",
    )
    parser.add_argument(
        "--verify-ssl",
        action="store_true",
        help="Verify Prism Central TLS certificate. Default: disabled.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="VM list page size. Default: 100",
    )
    parser.add_argument(
        "--filter-type",
        type=str,
        default="name",
        help="category/name. If Category is selected, the category needed is sbfix_needed:true(hardcoded)",
    )
    parser.add_argument(
        "--filter-value",
        type=str,
        default="name",
        help="Part of the VM name",
    )
    return parser.parse_args()


def build_vm_api(args: argparse.Namespace) -> VmApi:
    password = args.password or getpass.getpass("Prism Central password: ")

    configuration = VmConfiguration()
    configuration.host = f"{args.pc}"
    configuration.port = args.port
    configuration.username = args.username
    configuration.password = password
    configuration.verify_ssl = args.verify_ssl

    if not args.verify_ssl:
        configuration.ssl_ca_cert = None
        ssl._create_default_https_context = ssl._create_unverified_context

    return VmApi(api_client=VmApiClient(configuration=configuration))


def build_categories_api(args: argparse.Namespace) -> CategoriesApi:
    password = args.password or getpass.getpass("Prism Central password: ")

    configuration = CategoriesConfiguration()
    configuration.host = f"{args.pc}"
    configuration.port = args.port
    configuration.username = args.username
    configuration.password = password
    configuration.verify_ssl = args.verify_ssl

    if not args.verify_ssl:
        configuration.ssl_ca_cert = None
        ssl._create_default_https_context = ssl._create_unverified_context

    return CategoriesApi(api_client=CategoriesApiClient(configuration=configuration))

def build_tasks_api(args: argparse.Namespace) -> TasksApi:
    password = args.password or getpass.getpass("Prism Central password: ")

    configuration = TasksConfiguration()
    configuration.host = f"{args.pc}"
    configuration.port = args.port
    configuration.username = args.username
    configuration.password = password
    configuration.verify_ssl = args.verify_ssl

    if not args.verify_ssl:
        configuration.ssl_ca_cert = None
        ssl._create_default_https_context = ssl._create_unverified_context

    return TasksApi(api_client=TasksApiClient(configuration=configuration))


def list_all_vms(vm_api: VmApi, page_size: int) -> list[Any]:
    vms: list[Any] = []
    page = 0

    while True:
        response = vm_api.list_vms(_page=page, _limit=page_size)
        page_data = list(getattr(response, "data", None) or [])
        vms.extend(page_data)

        if len(vms) >= response.metadata.total_available_results:
            break
        if len(page_data) < page_size:
            break

        page += 1

    return vms


def list_all_categories(categories_api: CategoriesApi, page_size: int) -> list[Any]:
    categories: list[Any] = []
    page = 0

    while True:
        response = categories_api.list_categories(_page=page, _limit=page_size)
        page_data = list(getattr(response, "data", None) or [])
        categories.extend(page_data)

        if len(categories) >= response.metadata.total_available_results:
            break
        if len(page_data) < page_size:
            break

        page += 1

    return categories


def configure_quiet_output(verify_ssl: bool) -> None:
    logging.getLogger().setLevel(logging.WARNING)
    for logger_name in (
        "ntnx_vmm_py_client",
        "ntnx_vmm_py_client.rest",
        "ntnx_prism_py_client",
        "ntnx_prism_py_client.rest",
        "urllib3",
        "urllib3.connectionpool",
    ):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    if not verify_ssl:
        warnings.filterwarnings(
            "ignore", category=urllib3.exceptions.InsecureRequestWarning
        )


def create_logger(LOG_DIR) -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"sbfix_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    logger = logging.getLogger("sbfix")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter("%(levelname)s: %(message)s")
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    return logger


def normalize_display_value(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "value"):
        value = value.value
    return str(value).strip()


def nested_value(source: Any, *attrs: str) -> Any:
    current = source
    for attr in attrs:
        if current is None:
            return None
        current = getattr(current, attr, None)
    return current


def bool_state(value: Any) -> str:
    display_value = normalize_display_value(value)
    if not display_value:
        return "unknown"
    return display_value


def render_power_state_table(vms: list[Any]) -> str:
    headers = ["VM Name", "Power State", "Secure Boot", "vTPM"]
    rows = [
        [
            str(getattr(vm, "name", "")),
            normalize_display_value(getattr(vm, "power_state", "")),
            bool_state(nested_value(vm, "boot_config", "is_secure_boot_enabled")),
            bool_state(nested_value(vm, "vtpm_config", "is_vtpm_enabled")),
        ]
        for vm in vms
    ]
    column_width = max(len(cell) for row in [headers, *rows] for cell in row)

    def render_border() -> str:
        return "+" + "+".join("-" * (column_width + 2) for _ in headers) + "+"

    def render_row(values: list[str]) -> str:
        return "| " + " | ".join(value.ljust(column_width) for value in values) + " |"

    lines = [render_border(), render_row(headers), render_border()]
    lines.extend(render_row(row) for row in rows)
    lines.append(render_border())
    return "\n".join(lines)

def ask_powered_on_action(vm_list, powered_on_vm_list) -> str:
    while True:
        answer = (
            input(
                f"Some of the listed VMs ({len(vm_list)}) are Powered ON ({len(powered_on_vm_list)}). Should we shut them down and patch them or skip them? [shutdown/skip]: "
            )
            .strip()
            .lower()
        )
        if answer in {"shutdown", "shut down", "s", "yes", "y"}:
            return "shutdown"
        if answer in {"skip", "no", "n"}:
            return "skip"
        print("Please answer shutdown or skip.")


def confirm_action(prompt: str) -> bool:
    while True:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
        if not answer:
            return False
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def shutdown_vm(log, vm_api, tasks_api, vm) -> str:
    retries = 3
    for attempt in range(retries):  
        vm_name = vm.name
        ext_id = vm.ext_id

        # get the VM configuration
        api_response = get_vm(vm_api, ext_id)
        # Extract E-Tag Header
        etag_value = VmApiClient.get_etag(api_response)

        try:
            response = vm_api.shutdown_vm(extId=ext_id, if_match=etag_value)
        except Exception as e:
            log.error(f"Failed to shutdown VM {ext_id}: {e}")
            if attempt < retries - 1:
                    continue
            return "failure"
        
        # extract task id from response
        task_id = response.data.ext_id
        log.debug(f"Shutdown initiated for VM {vm_name}, task ID: {task_id}")
        
        # wait for task completion
        while True:
            task_status = get_task_status(log, tasks_api, task_id)
            if task_status == "succeeded":
                log.debug(f"Shutdown task succeeded for VM {vm_name}")
                break
            elif task_status == "failed":
                log.error(f"Shutdown failed for VM {vm_name}")
                break

        if attempt < retries - 1 and task_status == "failed":
            continue
        exit(1)
        
        retries = 0
        while True:
            retries += 1
            current_power_state = get_current_power_state(log, vm_api, vm)
            if current_power_state == "OFF":
                log.info(f"VM {vm_name} is now powered off.")
                return "success"
            elif current_power_state == "ON":
                log.debug(f"VM {vm_name} is still powering off...")
            else:
                log.warning(f"Unexpected power state '{current_power_state}' for VM {vm_name}.")
            time.sleep(5)
            if retries >= 60:  # Wait up to 5 minutes for the VM to power off
                log.error(f"VM {vm_name} did not power off within expected time.")
                return "failure"

def get_task_status(log, tasks_api, task_id) -> str:
    retries = 3
    for attempt in range(retries):    
        while True:
            try:
                task_response = tasks_api.get_task_by_id(extId=task_id)
                task_state = task_response.data.status
                log.debug(f"Task {task_id} state: {task_state}")
                if task_state in {"SUCCEEDED", "FAILED"}:
                    return task_state.lower()
            except Exception as e:
                log.error(f"Failed to get status for task {task_id}: {e}")
                if attempt < retries - 1:
                    break
                return "failure"
            time.sleep(5)

def get_vm(vm_api: VmApi, ext_id) -> list[Any]:

    response = vm_api.get_vm_by_id(extId=ext_id)

    return response

def deactivate_secure_boot(log, vm_api, tasks_api, vm) -> str:
    
    retries = 3
    for attempt in range(retries):

        vm_name = vm.name
        ext_id = vm.ext_id
        # get the VM configuration
        api_response = get_vm(vm_api, ext_id)
        # Extract E-Tag Header
        etag_value = VmApiClient.get_etag(api_response)

        # extract VM data from response
        data = api_response.data

        # path the secure boot configuration
        if data.boot_config is None:
            log.warning(f"VM {vm_name} has no boot configuration, skipping secure boot deactivation.")
            log.error("Fata Error - The VM has no boot configuration.")
            if attempt < retries - 1:
                continue
            exit(1)

        data.boot_config.is_secure_boot_enabled = False
        
        try:
            response = vm_api.update_vm_by_id(extId=ext_id, body=data, if_match=etag_value)
        except Exception as e:
            log.error(f"Failed to deactivate secure boot for VM {vm_name}: {e}")
            if attempt < retries - 1:
                continue
            return "failure"
        
        # extract task id from response
        task_id = response.data.ext_id
        log.debug(f"Secure boot deactivation initiated for VM {vm_name}, task ID: {task_id}")
        
        # wait for task completion
        while True:
            task_status = get_task_status(log, tasks_api, task_id)
            if task_status == "succeeded":
                log.debug(f"Secure boot deactivation succeeded for VM {vm_name}")
                return "success"
            elif task_status == "failed":
                log.error(f"Secure boot deactivation failed for VM {vm_name}")
                break

        if attempt < retries - 1 and task_status == "failed":
            continue
            
        exit(1)


def reactivate_secure_boot_and_vtpm(log, vm_api, tasks_api, vm) -> str:
    retries = 3
    for attempt in range(retries):
        vm_name = vm.name
        ext_id = vm.ext_id
        # get the VM configuration
        api_response = get_vm(vm_api, ext_id)
        # Extract E-Tag Header
        etag_value = VmApiClient.get_etag(api_response)

        # extract VM data from response
        data = api_response.data

        # path the secure boot configuration
        if data.boot_config is None:
            log.warning(f"VM {vm_name} has no boot configuration, skipping secure boot deactivation.")
            log.error("Fata Error - The VM has no boot configuration.")
            if attempt < retries - 1:
                continue
            exit(1)

        data.boot_config.is_secure_boot_enabled = True
        data.vtpm_config.is_vtpm_enabled = True

        try:
            response = vm_api.update_vm_by_id(extId=ext_id, body=data, if_match=etag_value)
        except Exception as e:
            log.error(f"Failed to reactivate secure boot and vTPM for VM {vm_name}: {e}")
            if attempt < retries - 1:
                continue
            return "failure"
        
        # extract task id from response
        task_id = response.data.ext_id
        log.debug(f"Secure boot and vTPM reactivation initiated for VM {vm_name}, task ID: {task_id}")
        
        # wait for task completion
        while True:
            task_status = get_task_status(log, tasks_api, task_id)
            if task_status == "succeeded":
                log.debug(f"Secure boot and vTPM reactivation succeeded for VM {vm_name}")
                return "success"
            elif task_status == "failed":
                log.error(f"Secure boot and vTPM reactivation failed for VM {vm_name}")
                break

        if attempt < retries - 1 and task_status == "failed":
            continue
        
        exit(1)

def add_vm_to_category(log, vm_api, tasks_api, vm, categories) -> str:

    retries = 3
    for attempt in range(retries):
        vm_name = vm.name
        ext_id = vm.ext_id
        # get the VM configuration
        api_response = get_vm(vm_api, ext_id)
        # Extract E-Tag Header
        etag_value = VmApiClient.get_etag(api_response)
        
        ahvConfigAssociateVmCategoriesParams = AhvConfigAssociateVmCategoriesParams()
        ahvConfigAssociateVmCategoriesParams.categories = []
        for category in categories:
            ahvConfigAssociateVmCategoriesParams.categories.append(category)
        try:
            # Add VM to category
            update_response = vm_api.associate_categories(extId=ext_id, body=ahvConfigAssociateVmCategoriesParams, if_match=etag_value)
            log.info(f"VM {vm_name} added to category '{category.key}' successfully.")
        except Exception as e:
            log.error(f"Failed to add VM {vm_name} to category '{category.key}': {e}")
            if attempt < retries - 1:
                continue
            return "failure"

        # extract task id from response
        task_id = update_response.data.ext_id
        log.debug(f"Category association initiated for VM {vm_name}, task ID: {task_id}")
        
        # wait for task completion
        while True:
            task_status = get_task_status(log, tasks_api, task_id)
            if task_status == "succeeded":
                log.info(f"VM {vm_name} successfully added to category '{category.key}'.")
                return "success"
            elif task_status == "failed":
                log.error(f"Failed to add VM {vm_name} to category '{category.key}'.")
                break

        if attempt < retries - 1 and task_status == "failed":
            continue
        exit(1)

def remove_vm_from_category(log, vm_api, tasks_api, vm, categories) -> str:
    retries = 3
    for attempt in range(retries):

        vm_name = vm.name
        ext_id = vm.ext_id
        # get the VM configuration
        api_response = get_vm(vm_api, ext_id)
        # Extract E-Tag Header
        etag_value = VmApiClient.get_etag(api_response)
        
        ahvConfigAssociateVmCategoriesParams = AhvConfigAssociateVmCategoriesParams()
        ahvConfigAssociateVmCategoriesParams.categories = []
        for category in categories:
            ahvConfigAssociateVmCategoriesParams.categories.append(category)
        try:
            # Add VM to category
            update_response = vm_api.disassociate_categories(extId=ext_id, body=ahvConfigAssociateVmCategoriesParams, if_match=etag_value)
            log.info(f"VM {vm_name} removed from category '{category.key}' successfully.")
        except Exception as e:
            log.error(f"Failed to remove VM {vm_name} from category '{category.key}': {e}")
            if attempt < retries - 1:
                    continue
            return "failure"

        # extract task id from response
        task_id = update_response.data.ext_id
        log.info(f"Category deassociation initiated for VM {vm_name}, task ID: {task_id}")
        
        # wait for task completion
        while True:
            task_status = get_task_status(log, tasks_api, task_id)
            if task_status == "succeeded":
                log.info(f"VM {vm_name} successfully removed from category '{category.key}'.")
                return "success"
            elif task_status == "failed":
                log.error(f"Failed to add VM {vm_name} to category '{category.key}'.")
                break

            if attempt < retries - 1 and task_status == "failed":
                continue
            exit(1)

def get_current_power_state(log, vm_api, vm) -> str:
    retries = 3
    for attempt in range(retries):
        ext_id = vm.ext_id
        try:
            api_response = get_vm(vm_api, ext_id)
            power_state = api_response.data.power_state
            log.debug(f"Current power state of VM {vm.name}: {power_state}")
            return power_state
        except Exception as e:
            log.error(f"Failed to get current power state for VM {vm.name}: {e}")
            if attempt < retries - 1:
                continue
            return "unknown"