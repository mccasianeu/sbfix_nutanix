#!/usr/bin/env python3
import argparse
import csv
from datetime import datetime
from pathlib import Path

from functions import (
    build_categories_api,
    build_vm_api,
    configure_quiet_output,
    list_all_categories,
    list_all_vms,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a CSV inventory report for VMs in Prism Central."
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
        help="VM/category list page size. Default: 100",
    )
    parser.add_argument(
        "--filter-type",
        type=str,
        default="all",
        choices=["all", "name", "category"],
        help="Filter mode: all, name, or category",
    )
    parser.add_argument(
        "--filter-value",
        type=str,
        default="",
        help="For name filter: substring. For category filter: key:value (default sbfix_needed:true)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./log"),
        help="Directory for CSV output. Default: ./log",
    )
    return parser.parse_args()


def normalize_output_dir(output_dir: Path) -> Path:
    normalized = str(output_dir).strip().rstrip("}")
    if not normalized:
        normalized = "./log"
    return Path(normalized)


def main() -> int:
    args = parse_args()
    configure_quiet_output(args.verify_ssl)

    vm_api = build_vm_api(args)
    categories_api = build_categories_api(args)

    print("Collecting VMs and categories from Prism Central...")
    all_vms = list_all_vms(vm_api, args.page_size)
    all_categories = list_all_categories(categories_api, args.page_size)

    category_by_ext_id = {
        category.ext_id: f"{category.key}:{category.value}"
        for category in all_categories
        if getattr(category, "ext_id", None)
    }

    sbfix_needed_false = next(
        (
            category
            for category in all_categories
            if category.key == "sbfix_needed" and category.value == "false"
        ),
        None,
    )
    sbfix_needed_false_id = sbfix_needed_false.ext_id if sbfix_needed_false else None
    category_filter_value = args.filter_value.strip() or "sbfix_needed:true"

    output_dir = normalize_output_dir(args.output_dir)
    if output_dir != args.output_dir:
        print(f"Normalized output dir from {args.output_dir} to {output_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = output_dir / f"sbfix_report_{timestamp}.csv"

    broken_vm_count = 0
    written = 0
    with report_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["vm_name", "categories", "patch_status", "power_state", "is_broken"])

        for vm in sorted(all_vms, key=lambda item: getattr(item, "name", "").lower()):
            vm_name = getattr(vm, "name", "")
            vm_category_ids = [cat.ext_id for cat in (vm.categories or []) if getattr(cat, "ext_id", None)]
            vm_categories = [category_by_ext_id.get(cat_id, cat_id) for cat_id in vm_category_ids]

            if args.filter_type == "name" and args.filter_value not in vm_name:
                continue
            if args.filter_type == "category" and category_filter_value not in vm_categories:
                continue

            patch_status = (
                "patched"
                if sbfix_needed_false_id and sbfix_needed_false_id in vm_category_ids
                else "not_patched"
            )
            is_broken = getattr(vm, "project", None) is None
            if is_broken:
                broken_vm_count += 1

            writer.writerow(
                [
                    vm_name,
                    ";".join(vm_categories),
                    patch_status,
                    getattr(vm, "power_state", ""),
                    "yes" if is_broken else "no",
                ]
            )
            written += 1

    print(f"Report generated: {report_path}")
    print(f"Total VMs written: {written}")
    print(f"Broken VMs in report: {broken_vm_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
