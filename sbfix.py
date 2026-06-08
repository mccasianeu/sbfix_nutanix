#!/usr/bin/env python3
from functions import (
    build_vm_api,
    build_categories_api,
    list_all_vms,
    list_all_categories,
    render_power_state_table,
    ask_powered_on_action,
    confirm_action,
    nested_value,
    shutdown_vm,
    create_logger,
    configure_quiet_output,
    parse_args,
    deactivate_secure_boot,
    reactivate_secure_boot_and_vtpm,
    build_tasks_api,
    add_vm_to_category,
    remove_vm_from_category,
    get_current_power_state
)


import logging
from pathlib import Path
LOG_DIR = Path("./log")


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
        "sbfix_needed_true": next((c for c in all_categories if c.key == "sbfix_needed" and c.value == "true"), None,),
        "sbfix_needed_false": next((c for c in all_categories if c.key == "sbfix_needed" and c.value == "false"), None,),
        "sbfix_sb_deactivated_true": next((c for c in all_categories if c.key == "sbfix_sb_deactivated" and c.value == "true"), None,),
        "sbfix_secure_boot_reactivated_true": next((c for c in all_categories if c.key == "sbfix_secure_boot_reactivated" and c.value == "true"), None,),
        "sbfix_vm_shutdown_true": next((c for c in all_categories if c.key == "sbfix_vm_shutdown" and c.value == "true"), None,),
        "sbfix_vtpm_activated_true": next((c for c in all_categories if c.key == "sbfix_vtpm_activated" and c.value == "true"), None,),
    }

    # filter VMs based on category or name, depending on the filter argument. 
    fix_needed_vms = []
    for vm in all_vms:
        log.debug(f"VM: {vm.name}")

        if args.filter == "category":
            if not vm.categories or categories["sbfix_needed_true"].ext_id not in [cat.ext_id for cat in vm.categories]:
                log.debug("  Not tagged with sbfix_needed:true, skipping")
                continue
        
        if args.filter == "name":
            if not vm.name.startswith("vba1q") and not vm.name.startswith("vba1a"):
            # if not vm.name in ["vba1uz990068", "vba1uz990069", "vba1uz990070", "vba1uz990071", "vba1uz990072", "vba1uz990073", "vba1uz990074", "vba1uz990075", "vba1uz990076", "vba1uz990077"]:
                log.debug("  VM name does not start with vba1q or vba1a, skipping")
                continue

        # skip the VMs that are already patched:
        if vm.categories and categories["sbfix_needed_false"].ext_id in [cat.ext_id for cat in vm.categories]:
            log.debug("  Already tagged with sbfix_needed:false, skipping")
            continue

        fix_needed_vms.append(vm)

    log.info(f"Found {len(fix_needed_vms)} VMs to be targeted for SBfix based on filter: {args.filter}")

    if fix_needed_vms:
        print(f"\nVMs filtered using: {args.filter}")
        print(render_power_state_table(fix_needed_vms))

    powered_on_vms = [vm for vm in fix_needed_vms if vm.power_state == "ON"]
    skipped_powered_on_vms = False
    if powered_on_vms:
        powered_on_action = ask_powered_on_action(fix_needed_vms, powered_on_vms)
        if powered_on_action == "skip":
            powered_on_ids = {vm.ext_id for vm in powered_on_vms}
            fix_needed_vms = [
                vm for vm in fix_needed_vms if vm.ext_id not in powered_on_ids
            ]
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

        if not confirm_action(f"Proceed with SBfix for the {len(fix_needed_vms)} VMs listed above?"):
            print("Stopping before applying SBfix.")
            raise SystemExit(0)
    else:
            if not confirm_action(f"Proceed with SBfix for the {len(fix_needed_vms)} VMs listed above?"):
                print("Stopping before applying SBfix.")
                raise SystemExit(0)

    print("Proceeding with the following list of VMs to be patched:")
    vm_index = 0
    for selected_vm in fix_needed_vms:
        vm_index = vm_index + 1
        log.info(
            f"Processing VM {vm_index}/{len(fix_needed_vms)} - "
            f"VM: {selected_vm.name} "
            f"(Power State: {selected_vm.power_state}, "
            f"Secure Boot: {nested_value(selected_vm, 'boot_config', 'is_secure_boot_enabled')}, "
            f"vTPM: {nested_value(selected_vm, 'vtpm_config', 'is_vtpm_enabled')})"
        )

        if selected_vm.power_state == "ON": # we assume no powered on VMs remain if the user chose to skip them, therefore if something is powered on here, it means the user chose to shut them down, so we proceed with shutdown without asking again.
            log.info(f"VM {selected_vm.name}  - Shutting down VM")
            task_id = shutdown_vm(log, vm_api, tasks_api, selected_vm)

            # check the shutting down task status until it's completed before proceeding with the rest of the SBfix steps, to avoid doing the rest of the steps while the VM is still shutting down, which could cause issues. We can check the task status by polling the VM's power state until it shows as powered off, or by checking the task status if the API provides that information.
            log.info(f"VM {selected_vm.name}  - Waiting for VM to be powered off before proceeding with SBfix steps")

            log.info(f"VM {selected_vm.name}  - adding the sbfix_vm_shutdown: True category to indicate the VM was shut down as part of the SBfix run")
        
        # re-check whatever the VM is Powered On or Off, and if it's still on, skip it
        current_power_state = get_current_power_state(log, vm_api, selected_vm)
        if current_power_state != "OFF":
            log.error(f"VM {selected_vm.name}  - VM is ON; expected state Off. Asssuming it was powered on in the meantime... skipping it.")
            continue


        # Secure Boot deactivation
        log.info(f"VM {selected_vm.name}  - Deactivating Secure Boot if enabled")
        post_check_result = deactivate_secure_boot(log, vm_api, tasks_api, selected_vm)
        log.info(f"VM {selected_vm.name}  - Post-Check: Verifying Secure Boot is disabled: {post_check_result}")
        if post_check_result == "failed":
            log.error(f"VM {selected_vm.name}  - SBfix failed at Secure Boot deactivation step. Cannot continue")
            exit(1)
        
        # adding sbfix_sb_deactivated_true
        log.info(f"VM {selected_vm.name}  - Adding the sbfix_sb_deactivated:true category to indicate SB was deactivated as part of the SBfix run")
        post_check_result = add_vm_to_category(log, vm_api, tasks_api, selected_vm, [categories["sbfix_sb_deactivated_true"]])
        log.debug(f"VM {selected_vm.name}  - Post-Check: Verifying sbfix_sb_deactivated_true category was added: {post_check_result}")
        if post_check_result == "failed":
            log.error(f"VM {selected_vm.name}  - SBfix failed at adding sbfix_sb_deactivated_true step. Cannot continue")
            exit(1)

        # Secure Boot re-activation and vTPM activation
        log.info(f"VM {selected_vm.name}  - Reactivating Secure Boot and activating vTPM")
        post_check_result = reactivate_secure_boot_and_vtpm(log, vm_api, tasks_api, selected_vm)
        log.debug(f"VM {selected_vm.name}  - Post-Check: Verifying Secure Boot is enabled and vTPM is enabled: {post_check_result}")
        if post_check_result == "failed":
            log.error(f"VM {selected_vm.name}  - SBfix failed at Secure Boot re-activation and vTPM activation step. Cannot continue")
            exit(1)
        
        # adding sbfix_secure_boot_reactivated_true and sbfix_vtpm_activated_true
        log.info(f"VM {selected_vm.name}  - Adding the sbfix_secure_boot_reactivated:true and sbfix_vtpm_activated:true categories to indicate SB was reactivated and vTPM was activated as part of the SBfix run")
        post_check_result = add_vm_to_category(log, vm_api, tasks_api, selected_vm, [categories["sbfix_secure_boot_reactivated_true"], categories["sbfix_vtpm_activated_true"], categories["sbfix_needed_false"]])
        log.debug(f"VM {selected_vm.name}  - Post-Check: Verifying sbfix_secure_boot_reactivated_true, sbfix_vtpm_activated_true and sbfix_needed_false categories were added: {post_check_result}")
        if post_check_result == "failed":
            log.error(f"VM {selected_vm.name}  - SBfix failed at adding sbfix_secure_boot_reactivated_true and sbfix_vtpm_activated_true step. Cannot continue")
            exit(1)

        #removing the sbfix_needed:true category and adding sbfix_needed:false category to indicate the VM has been processed, and adding other categories to indicate what actions were taken and what the post-check results were. The exact categories to add will depend on the final category design.
        if vm.categories and categories["sbfix_needed_true"].ext_id in [cat.ext_id for cat in vm.categories]:
            log.info(f"VM {selected_vm.name}  - Updating categories to reflect SBfix run results")
            post_check_result = remove_vm_from_category(log, vm_api, tasks_api, selected_vm [categories["sbfix_needed_true"]])
            log.debug(f"VM {selected_vm.name}  - Post-Check: Verifying sbfix_needed_true category was removed: {post_check_result}")
            if post_check_result == "failed":
                log.error(f"VM {selected_vm.name}  - SBfix failed at removing sbfix_needed_true category step. Cannot continue")
                exit(1)

        log.info(f"VM {selected_vm.name}  - SBfix completed successfully for this VM")
        log.info("-" * 50)
    log.info("SBfix run completed")
