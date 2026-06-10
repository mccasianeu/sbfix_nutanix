#!/usr/bin/env python3
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import logging
import time
from pathlib import Path

from functions import (
    add_vm_to_category,
    ask_powered_on_action,
    build_categories_api,
    build_tasks_api,
    build_vm_api,
    configure_quiet_output,
    confirm_action,
    create_logger,
    deactivate_secure_boot,
    get_current_power_state,
    list_all_categories,
    list_all_vms,
    nested_value,
    parse_args,
    reactivate_secure_boot_and_vtpm,
    remove_vm_from_category,
    render_power_state_table,
    shutdown_vm,
    write_summary_log
)

LOG_DIR = Path("./log")
MAX_PARALLEL_TASKS = 10
TASK_START_INTERVAL_SECONDS = 2


def process_vm(selected_vm, vm_index, total_vms, log, vm_api, tasks_api, categories):
    log.info(
        f"Processing VM {vm_index}/{total_vms} - "
        f"VM: {selected_vm.name} "
        f"(Power State: {selected_vm.power_state}, "
        f"Secure Boot: {nested_value(selected_vm, 'boot_config', 'is_secure_boot_enabled')}, "
        f"vTPM: {nested_value(selected_vm, 'vtpm_config', 'is_vtpm_enabled')})"
    )

    try:
        if selected_vm.project is None:
            message = (
                f"VM {selected_vm.name}  - No project assigned, skipping VM as SBfix requires the VM to be in a project"
            )
            log.warning(message)
            return {"vm": selected_vm.name, "status": "skipped", "reason": message}

        if selected_vm.power_state == "ON":
            log.info(f"VM {selected_vm.name}  - Shutting down VM")
            shutdown_result = shutdown_vm(log, vm_api, tasks_api, selected_vm)
            if shutdown_result in {"failed", "failure"}:
                message = f"VM {selected_vm.name}  - Shutdown failed"
                log.error(message)
                return {"vm": selected_vm.name, "status": "failed", "reason": message}

            log.info(
                f"VM {selected_vm.name}  - Waiting for VM to be powered off before proceeding with SBfix steps"
            )
            log.info(
                f"VM {selected_vm.name}  - adding the sbfix_vm_shutdown: True category to indicate the VM was shut down as part of the SBfix run"
            )

        current_power_state = get_current_power_state(log, vm_api, selected_vm)
        if current_power_state != "OFF":
            message = (
                f"VM {selected_vm.name}  - VM is ON; expected state Off. Asssuming it was powered on in the meantime... skipping it."
            )
            log.error(message)
            return {"vm": selected_vm.name, "status": "skipped", "reason": message}

        log.info(f"VM {selected_vm.name}  - Deactivating Secure Boot if enabled")
        post_check_result = deactivate_secure_boot(log, vm_api, tasks_api, selected_vm)
        log.info(
            f"VM {selected_vm.name}  - Post-Check: Verifying Secure Boot is disabled: {post_check_result}"
        )
        if post_check_result in {"failed", "failure"}:
            message = f"VM {selected_vm.name}  - SBfix failed at Secure Boot deactivation step"
            log.error(message)
            return {"vm": selected_vm.name, "status": "failed", "reason": message}

        log.info(
            f"VM {selected_vm.name}  - Adding the sbfix_sb_deactivated:true category to indicate SB was deactivated as part of the SBfix run"
        )
        post_check_result = add_vm_to_category(
            log,
            vm_api,
            tasks_api,
            selected_vm,
            [categories["sbfix_sb_deactivated_true"]],
        )
        log.debug(
            f"VM {selected_vm.name}  - Post-Check: Verifying sbfix_sb_deactivated_true category was added: {post_check_result}"
        )
        if post_check_result in {"failed", "failure"}:
            message = f"VM {selected_vm.name}  - SBfix failed at adding sbfix_sb_deactivated_true step"
            log.error(message)
            return {"vm": selected_vm.name, "status": "failed", "reason": message}

        log.info(f"VM {selected_vm.name}  - Reactivating Secure Boot and activating vTPM")
        post_check_result = reactivate_secure_boot_and_vtpm(log, vm_api, tasks_api, selected_vm)
        log.debug(
            f"VM {selected_vm.name}  - Post-Check: Verifying Secure Boot is enabled and vTPM is enabled: {post_check_result}"
        )
        if post_check_result in {"failed", "failure"}:
            message = (
                f"VM {selected_vm.name}  - SBfix failed at Secure Boot re-activation and vTPM activation step"
            )
            log.error(message)
            return {"vm": selected_vm.name, "status": "failed", "reason": message}

        log.info(
            f"VM {selected_vm.name}  - Adding the sbfix_secure_boot_reactivated:true and sbfix_vtpm_activated:true categories to indicate SB was reactivated and vTPM was activated as part of the SBfix run"
        )
        post_check_result = add_vm_to_category(
            log,
            vm_api,
            tasks_api,
            selected_vm,
            [
                categories["sbfix_secure_boot_reactivated_true"],
                categories["sbfix_vtpm_activated_true"],
                categories["sbfix_needed_false"],
            ],
        )
        log.debug(
            f"VM {selected_vm.name}  - Post-Check: Verifying sbfix_secure_boot_reactivated_true, sbfix_vtpm_activated_true and sbfix_needed_false categories were added: {post_check_result}"
        )
        if post_check_result in {"failed", "failure"}:
            message = (
                f"VM {selected_vm.name}  - SBfix failed at adding sbfix_secure_boot_reactivated_true and sbfix_vtpm_activated_true step"
            )
            log.error(message)
            return {"vm": selected_vm.name, "status": "failed", "reason": message}

        if selected_vm.categories and categories["sbfix_needed_true"].ext_id in [
            cat.ext_id for cat in selected_vm.categories
        ]:
            log.info(f"VM {selected_vm.name}  - Updating categories to reflect SBfix run results")
            post_check_result = remove_vm_from_category(
                log,
                vm_api,
                tasks_api,
                selected_vm,
                [categories["sbfix_needed_true"]],
            )
            log.debug(
                f"VM {selected_vm.name}  - Post-Check: Verifying sbfix_needed_true category was removed: {post_check_result}"
            )
            if post_check_result in {"failed", "failure"}:
                message = f"VM {selected_vm.name}  - SBfix failed at removing sbfix_needed_true category step"
                log.error(message)
                return {"vm": selected_vm.name, "status": "failed", "reason": message}

        log.info(f"VM {selected_vm.name}  - SBfix completed successfully for this VM")
        log.info("-" * 50)
        log.info("")
        return {"vm": selected_vm.name, "status": "success", "reason": ""}
    except SystemExit as exc:
        message = f"VM {selected_vm.name}  - Worker stopped with SystemExit({exc.code})"
        log.error(message)
        return {"vm": selected_vm.name, "status": "failed", "reason": message}
    except Exception as exc:
        message = f"VM {selected_vm.name}  - Unexpected error: {exc}"
        log.exception(message)
        return {"vm": selected_vm.name, "status": "failed", "reason": message}


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    args = parse_args()
    configure_quiet_output(args.verify_ssl)

    log = create_logger(LOG_DIR)
    log.info("Starting")
    vm_api = build_vm_api(args)
    categories_api = build_categories_api(args)
    tasks_api = build_tasks_api(args)

    print("Collecting VMs from Prism Central...")
    all_vms = list_all_vms(vm_api, args.page_size)
    all_categories = list_all_categories(categories_api, args.page_size)
    categories = {
        "sbfix_needed_true": next((c for c in all_categories if c.key == "sbfix_needed" and c.value == "true"), None),
        "sbfix_needed_false": next((c for c in all_categories if c.key == "sbfix_needed" and c.value == "false"), None),
        "sbfix_sb_deactivated_true": next((c for c in all_categories if c.key == "sbfix_sb_deactivated" and c.value == "true"), None),
        "sbfix_secure_boot_reactivated_true": next((c for c in all_categories if c.key == "sbfix_secure_boot_reactivated" and c.value == "true"), None),
        "sbfix_vm_shutdown_true": next((c for c in all_categories if c.key == "sbfix_vm_shutdown" and c.value == "true"), None),
        "sbfix_vtpm_activated_true": next((c for c in all_categories if c.key == "sbfix_vtpm_activated" and c.value == "true"), None),
    }

    fix_needed_vms = []
    filtered_vms = []

    sbfix_needed_false_id = categories["sbfix_needed_false"].ext_id if categories["sbfix_needed_false"] else None
    patched_vms = [vm for vm in all_vms if sbfix_needed_false_id and vm.categories and sbfix_needed_false_id in [cat.ext_id for cat in vm.categories]]
    unpatched_vms = [vm for vm in all_vms if not (sbfix_needed_false_id and vm.categories and sbfix_needed_false_id in [cat.ext_id for cat in vm.categories])]

    for vm in all_vms:
        log.debug(f"VM: {vm.name}")

        if args.filter_type == "category":
            if not vm.categories or categories["sbfix_needed_true"].ext_id not in [cat.ext_id for cat in vm.categories]:
                log.debug("  Not tagged with sbfix_needed:true, skipping")
                continue

        if args.filter_type == "name":
            if args.filter_value not in vm.name:
                log.debug(f"  VM name does not contain {args.filter_value}, skipping")
                continue

        filtered_vms.append(vm)

        if sbfix_needed_false_id and vm.categories and sbfix_needed_false_id in [cat.ext_id for cat in vm.categories]:
            log.debug("  Already tagged with sbfix_needed:false, skipping")
            continue

        fix_needed_vms.append(vm)

    log.info(
        f"Found {len(fix_needed_vms)} VMs to be targeted for SBfix based on filter type: {args.filter_type}"
    )

    filtered_vms_count = len(filtered_vms)

    if fix_needed_vms:
        print(f"\nVMs filtered using: {args.filter_type}")
        print(render_power_state_table(fix_needed_vms))

    powered_on_vms = [vm for vm in fix_needed_vms if vm.power_state == "ON"]
    skipped_powered_on_vms = False
    if powered_on_vms:
        if args.only_powered_off:
            powered_on_action = "skip"
            log.info("only-powered-off enabled: powered-on VMs will be skipped")
        else:
            powered_on_action = ask_powered_on_action(fix_needed_vms, powered_on_vms)
        if powered_on_action == "skip":
            powered_on_ids = {vm.ext_id for vm in powered_on_vms}
            fix_needed_vms = [vm for vm in fix_needed_vms if vm.ext_id not in powered_on_ids]
            skipped_powered_on_vms = True
            log.info(f"Skipping {len(powered_on_vms)} powered-on VMs")
        else:
            log.info(f"Will shut down and patch {len(powered_on_vms)} powered-on VMs")

    if skipped_powered_on_vms:
        if not fix_needed_vms:
            print("\nNo VMs remain to be patched after skipping powered-on VMs.")
            raise SystemExit(0)

        print("\nVMs selected for SBfix after skipping powered-on VMs:")
        print(render_power_state_table(fix_needed_vms))

    if args.auto_approve:
        log.info("auto-approve enabled: proceeding without confirmation prompt")
    elif not confirm_action(f"Proceed with SBfix for the {len(fix_needed_vms)} VMs listed above?"):
        print("Stopping before applying SBfix.")
        raise SystemExit(0)

    print("Proceeding with the following list of VMs to be patched:")

    results = []
    in_flight = {}
    total_vms = len(fix_needed_vms)

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_TASKS) as executor:
        for vm_index, selected_vm in enumerate(fix_needed_vms, start=1):
            while len(in_flight) >= MAX_PARALLEL_TASKS:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for completed in done:
                    results.append(completed.result())
                    del in_flight[completed]

            future = executor.submit(
                process_vm,
                selected_vm,
                vm_index,
                total_vms,
                log,
                vm_api,
                tasks_api,
                categories,
            )
            in_flight[future] = selected_vm.ext_id

            if vm_index < total_vms:
                time.sleep(TASK_START_INTERVAL_SECONDS)

        while in_flight:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for completed in done:
                results.append(completed.result())
                del in_flight[completed]

    success_count = sum(1 for result in results if result["status"] == "success")
    skipped_count = sum(1 for result in results if result["status"] == "skipped")
    failed_count = sum(1 for result in results if result["status"] == "failed")

    filtered_patched_vms = [vm for vm in filtered_vms if sbfix_needed_false_id and vm.categories and sbfix_needed_false_id in [cat.ext_id for cat in vm.categories]]
    filtered_unpatched_powered_on_vms = sum(
        1
        for vm in filtered_vms
        if not (sbfix_needed_false_id and vm.categories and sbfix_needed_false_id in [cat.ext_id for cat in vm.categories])
        and vm.power_state == "ON"
    )
    filtered_unpatched_powered_off_vms = sum(
        1
        for vm in filtered_vms
        if not (sbfix_needed_false_id and vm.categories and sbfix_needed_false_id in [cat.ext_id for cat in vm.categories])
        and vm.power_state == "OFF"
    )

    inventory_counts = {
        "filter_type": args.filter_type,
        "filter_used": "sbfix_needed:true" if args.filter_type == "category" else args.filter_value,
        "total_vms_in_pc": len(all_vms),
        "pc_patched_vms": len(patched_vms),
        "pc_not_yet_patched_vms": len(unpatched_vms),
        "filtered_vms": filtered_vms_count,
        "filtered_unpatched_powered_on_vms": filtered_unpatched_powered_on_vms,
        "filtered_unpatched_powered_off_vms": filtered_unpatched_powered_off_vms,
        "filtered_patched_vms": len(filtered_patched_vms),
    }

    summary_path = write_summary_log(LOG_DIR, results, inventory_counts)

    log.info(
        f"SBfix run completed - success: {success_count}, skipped: {skipped_count}, failed: {failed_count}"
    )
    log. info(f"Detailed summary written to {summary_path}")

    if failed_count > 0:
        raise SystemExit(1)
