#!/usr/bin/python
#
# Options Available:
#      --environment    values:  prod or beta
#      --course-id      Canvas course id (integer)
#      --group-dn       Source AD group distinguished name.
#      --apply          Apply adds/drops. If omitted, runs as dry-run.
#      --log-file       Optional log file path.

import argparse
import json
import logging
import sys
from collections import deque
from logging.handlers import RotatingFileHandler

import requests
from columnar import columnar
from ldap3 import ALL, BASE, Connection, Server

sys.path.append("/var/lib/canvas-mgmt/bin")
from canvasFunctions import getEnv

CANVAS_API_BY_ENV = {
    "production": "https://canvas.illinois.edu/api/v1",
    "beta": "https://illinoisedu.beta.instructure.com/api/v1",
}

def parse_args():
    parser = argparse.ArgumentParser(
        description="Sync AD group members to Canvas course StudentEnrollment roster."
    )
    parser.add_argument(
        "--env",
        dest="environment",
        choices=["production", "beta"],
        help="Canvas environment to target.",
    )
    parser.add_argument("--course-id", help="Canvas course ID (numeric Canvas ID).")
    parser.add_argument("--group-dn", help="Source AD group distinguished name.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply adds/drops. If omitted, runs as dry-run.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=30, help="HTTP timeout in seconds.")
    parser.add_argument("--log-file", default="", help="Optional log file path.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()

def prompt_for_missing_args(args):
    if not args.environment:
        while True:
            env_value = input("Environment (production/beta): ").strip().lower()
            if env_value in CANVAS_API_BY_ENV:
                args.environment = env_value
                break
            print("Invalid environment. Enter 'production' or 'beta'.")

    if not args.course_id:
        while True:
            course_id = input("Canvas course ID: ").strip()
            if course_id:
                args.course_id = course_id
                break
            print("Canvas course ID is required.")

    if not args.group_dn:
        while True:
            group_dn = input("AD group DN: ").strip()
            if group_dn:
                args.group_dn = group_dn
                break
            print("AD group DN is required.")

def configure_logging(level, log_file=""):
    logger = logging.getLogger("ad-canvas-sync")
    logger.setLevel(getattr(logging, level))
    logger.handlers = []

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file:
        file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger

def bind_ldap(ldap_host, bind_dn, bind_pw):
    server = Server(ldap_host, port=636, use_ssl=True, get_info=ALL)
    return Connection(
        server,
        user=bind_dn,
        password=bind_pw,
        auto_bind=True,
        read_only=True,
    )

def get_ad_group_netids(conn, group_dn):
    netids = set()
    seen_dns = set()
    queue = deque([group_dn])

    while queue:
        current_dn = queue.popleft()
        if current_dn in seen_dns:
            continue
        seen_dns.add(current_dn)

        conn.search(
            search_base=current_dn,
            search_filter="(objectClass=*)",
            search_scope=BASE,
            attributes=["objectClass", "member", "sAMAccountName"],
        )
        if not conn.entries:
            continue

        entry = conn.entries[0]
        object_classes = {str(v).lower() for v in entry["objectClass"].values} if "objectClass" in entry else set()

        if "user" in object_classes:
            sam = entry["sAMAccountName"].value if "sAMAccountName" in entry else None
            if sam:
                netids.add(str(sam).strip().lower())
            continue

        if "group" in object_classes and "member" in entry:
            for member_dn in entry["member"].values:
                queue.append(str(member_dn))

    return sorted(netids)

def canvas_get_enrollment_indexes(session, base_api, course_id, timeout_seconds):
    url = f"{base_api}/courses/{course_id}/enrollments"
    params = {
        "per_page": 100,
        "state[]": ["active", "invited"],
    }

    enrollments = []
    while url:
        r = session.get(url, params=params, timeout=timeout_seconds)
        r.raise_for_status()
        batch = r.json()
        enrollments.extend(batch)
        url = r.links.get("next", {}).get("url")
        params = {}

    student_index = {}
    any_role_netids = set()
    non_student_roles = {}

    for e in enrollments:
        user = e.get("user") or {}
        netid = (user.get("sis_user_id") or user.get("login_id") or "").strip().lower()
        role = (e.get("role") or "").strip()
        enrollment_id = e.get("id")
        if not netid:
            continue

        any_role_netids.add(netid)
        if role == "StudentEnrollment" and enrollment_id:
            student_index[netid] = enrollment_id
            continue

        if role:
            non_student_roles.setdefault(netid, set()).add(role)

    return student_index, any_role_netids, non_student_roles

def print_change_table(keep, adds, drops, blocked_from_add=None):
    blocked_from_add = blocked_from_add or {}
    rows = []
    for netid in sorted(keep):
        rows.append([netid, "Enrolled - No change", ""])
    for netid in sorted(blocked_from_add.keys()):
        roles = ", ".join(sorted(blocked_from_add[netid]))
        rows.append([netid, f"Already enrolled ({roles})", ""])
    for netid in sorted(adds):
        rows.append([netid, "New", ""])
    for netid, enrollment_id in sorted(drops.items()):
        rows.append([netid, "Drop Enrollment", str(enrollment_id)])

    if not rows:
        print("No enrollment changes detected.")
        return

    print(columnar(rows, ["NetID", "Status", "Enrollment_ID"], no_borders=True))

def apply_changes(session, base_api, course_id, adds, drops, timeout_seconds, logger):
    for netid in sorted(adds):
        enroll_url = f"{base_api}/courses/{course_id}/enrollments"
        payload = {
            "enrollment[user_id]": f"sis_user_id:{netid}",
            "enrollment[type]": "StudentEnrollment",
            "enrollment[enrollment_state]": "active",
            "enrollment[notify]": "false",
        }
        r = session.post(enroll_url, data=payload, timeout=timeout_seconds)
        r.raise_for_status()
        logger.info("Added enrollment for %s", netid)

    for netid, enrollment_id in sorted(drops.items()):
        drop_url = f"{base_api}/courses/{course_id}/enrollments/{enrollment_id}"
        r = session.delete(drop_url, params={"task": "conclude"}, timeout=timeout_seconds)
        r.raise_for_status()
        logger.info("Concluded enrollment for %s (enrollment_id=%s)", netid, enrollment_id)

def main():
    args = parse_args()
    prompt_for_missing_args(args)
    logger = configure_logging(args.log_level, args.log_file)

    env = getEnv()
    ldap_host = env.get("UofI.ldap.ad_sys")
    ldap_bind_dn = env.get("UofI.ad_bind")
    ldap_bind_pw = env.get("UofI.ad_bindpwd")

    if not ldap_host or not ldap_bind_dn or not ldap_bind_pw:
        raise RuntimeError("Missing LDAP config: UofI.ldap.ad_sys, UofI.ad_bind, UofI.ad_bindpwd")

    if args.environment == "beta":
        canvas_token = env.get("canvas.token-beta") or env.get("canvas.token")
    else:
        canvas_token = env.get("canvas.token")

    if not canvas_token:
        raise RuntimeError("Missing Canvas token (expected canvas.token or canvas.token-beta).")

    base_api = CANVAS_API_BY_ENV[args.environment]
    logger.info(
        "Starting sync environment=%s course_id=%s group_dn=%s apply=%s",
        args.environment,
        args.course_id,
        args.group_dn,
        args.apply,
    )

    ldap_conn = bind_ldap(ldap_host, ldap_bind_dn, ldap_bind_pw)

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {canvas_token}"})

    ad_netids = set(get_ad_group_netids(ldap_conn, args.group_dn))
    canvas_student_netid_to_enrollment, canvas_any_role_netids, non_student_role_map = canvas_get_enrollment_indexes(
        session, base_api, args.course_id, args.timeout_seconds
    )
    canvas_student_netids = set(canvas_student_netid_to_enrollment.keys())

    keep = ad_netids & canvas_student_netids
    blocked_from_add = {
        netid: non_student_role_map.get(netid, {"NonStudentEnrollment"})
        for netid in sorted((ad_netids - canvas_student_netids) & canvas_any_role_netids)
    }
    adds = ad_netids - canvas_any_role_netids
    drops = {
        netid: canvas_student_netid_to_enrollment[netid]
        for netid in (canvas_student_netids - ad_netids)
    }

    print_change_table(keep, adds, drops, blocked_from_add)

    summary = {
        "environment": args.environment,
        "course_id": args.course_id,
        "group_dn": args.group_dn,
        "mode": "apply" if args.apply else "dry-run",
        "ad_member_count": len(ad_netids),
        "canvas_member_count": len(canvas_student_netids),
        "canvas_any_role_member_count": len(canvas_any_role_netids),
        "unchanged_count": len(keep),
        "blocked_existing_role_count": len(blocked_from_add),
        "add_count": len(adds),
        "drop_count": len(drops),
    }
    logger.info("Run summary: %s", json.dumps(summary, sort_keys=True))

    if args.apply:
        apply_changes(session, base_api, args.course_id, adds, drops, args.timeout_seconds, logger)
        logger.info("Apply complete.")
    else:
        logger.info("Dry-run complete. No changes applied.")

    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        sys.exit(1)