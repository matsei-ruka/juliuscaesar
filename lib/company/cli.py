"""``jc company <subcommand>`` argparse layer.

Subcommands:

* ``register`` — interactive enrollment. Writes endpoint + token to ``.env``,
  calls ``/api/agents/register``, persists the returned API key.
* ``status`` — endpoint, agent_id, API-key presence, outbox depth, reporter run.
* ``alert`` — fire-and-forget alert POST.
* ``approval`` — raise + optionally block until decided.
* ``agents`` — same-company organigram / peer discovery.
* ``task`` — task graph read / create / update helpers.
* ``replay`` — drain ``state/company/outbox/`` to the backend.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import alerts as alerts_mod
from . import approvals as approvals_mod
from . import conf as conf_module
from .client import CompanyClient, CompanyError
from .reporter import Outbox, build_snapshot


# --- Helpers ----------------------------------------------------------------


def _resolve_instance(arg: str | None) -> Path:
    """Reuse jc's standard instance-dir resolution."""
    from jc_paths import InstanceResolutionError, resolve_instance_dir  # type: ignore

    try:
        return resolve_instance_dir(arg, fallback_markers=("memory",))
    except InstanceResolutionError as exc:
        raise SystemExit(str(exc)) from exc


def _print_kv(rows: list[tuple[str, Any]]) -> None:
    width = max(len(k) for k, _ in rows) + 2
    for key, value in rows:
        print(f"{key:<{width}}{value}")


def _parse_since(token: str) -> datetime:
    """Parse ``1h``, ``30m``, ``2d`` into an absolute UTC cutoff."""
    text = token.strip().lower()
    if not text:
        raise ValueError("empty --since")
    unit = text[-1]
    try:
        n = int(text[:-1])
    except ValueError as exc:
        raise ValueError(f"--since must be like '1h', '30m', '2d': {token!r}") from exc
    delta = {
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
    }.get(unit)
    if delta is None:
        raise ValueError(f"--since unit must be s/m/h/d: {token!r}")
    return datetime.now(timezone.utc) - delta


def _load_json_arg(value: str | None, *, field: str) -> dict[str, Any]:
    """Parse an inline JSON object or @path JSON object.

    Empty / missing values map to ``{}``. This keeps CLI calls idempotent
    and avoids forcing agents to create temporary files for simple payloads.
    """
    if not value:
        return {}
    text: str
    if value.startswith("@"):
        path = Path(value[1:]).expanduser()
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise SystemExit(f"{field}: file not found: {path}") from exc
    else:
        text = value
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{field}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{field}: JSON must be an object")
    return data


def _client_for(instance: Path) -> CompanyClient:
    cfg = conf_module.load(instance)
    if not cfg.endpoint:
        raise SystemExit("COMPANY_ENDPOINT missing — run `jc company register` first")
    if not cfg.api_key:
        raise SystemExit("COMPANY_API_KEY missing — run `jc company register` first")
    return CompanyClient(cfg)


def _print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


# --- register --------------------------------------------------------------


def cmd_register(args: argparse.Namespace) -> int:
    instance = _resolve_instance(args.instance_dir)

    cfg_existing = conf_module.load(instance)
    endpoint = (args.endpoint or cfg_existing.endpoint or "").rstrip("/")
    if not endpoint:
        endpoint = input("Company endpoint (e.g. https://thecompany.example.com): ").strip().rstrip("/")
    if not endpoint:
        raise SystemExit("endpoint is required")

    token = args.token or cfg_existing.enrollment_token
    if not token:
        token = getpass.getpass("Enrollment token (input hidden): ").strip()
    if not token:
        raise SystemExit("enrollment token is required")

    name = args.name or conf_module.instance_name(instance)
    instance_id = conf_module.instance_id(instance)

    # Persist endpoint + token first so a re-run after a network blip uses
    # the same values without prompting.
    conf_module.write_env_keys(
        instance,
        set_keys={"COMPANY_ENDPOINT": endpoint, "COMPANY_ENROLLMENT_TOKEN": token},
    )

    cfg = conf_module.load(instance)
    client = CompanyClient(cfg)
    try:
        result = client.register(
            instance_id=instance_id,
            name=name,
            framework="juliuscaesar",
            framework_version=conf_module.framework_version(),
            enrollment_token=token,
        )
    except CompanyError as exc:
        raise SystemExit(f"register failed: status={exc.status} {exc}") from exc
    finally:
        client.close()

    api_key = result.get("api_key")
    agent_id = result.get("agent_id") or "?"
    if not api_key:
        raise SystemExit(f"register: server returned no api_key: {result}")

    conf_module.write_env_keys(
        instance,
        set_keys={"COMPANY_API_KEY": str(api_key)},
        unset_keys=("COMPANY_ENROLLMENT_TOKEN",),
    )

    print(f"registered: agent_id={agent_id} name={name!r} endpoint={endpoint}")
    print("API key saved to .env. Enrollment token removed.")
    return 0


# --- status ----------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    instance = _resolve_instance(args.instance_dir)
    cfg = conf_module.load(instance)

    outbox = Outbox(
        instance,
        max_mb=cfg.outbox_max_mb,
        max_age_hours=cfg.outbox_max_age_hours,
    )
    files = outbox.files()
    bytes_ = outbox.total_bytes()

    rows: list[tuple[str, Any]] = [
        ("endpoint", cfg.endpoint or "(unset)"),
        ("enabled", cfg.enabled),
        ("api_key", "set" if cfg.api_key else "missing"),
        ("enrollment_token", "set" if cfg.enrollment_token else "(none)"),
        ("instance_id", conf_module.instance_id(instance)),
        ("instance_name", conf_module.instance_name(instance)),
        ("framework_version", conf_module.framework_version()),
        ("redact_conversations", cfg.redact_conversations),
        ("exclude_channels", list(cfg.exclude_channels) or "[]"),
        ("exclude_users", list(cfg.exclude_users) or "[]"),
        ("outbox_files", len(files)),
        ("outbox_bytes", bytes_),
    ]
    _print_kv(rows)

    if not cfg.endpoint or not cfg.api_key:
        return 0

    if args.ping:
        client = CompanyClient(cfg)
        try:
            client.heartbeat({**build_snapshot(instance), "status": "online"})
            print("\nping: ok")
        except CompanyError as exc:
            print(f"\nping: FAILED status={exc.status} {exc}")
            return 1
        finally:
            client.close()
    return 0


# --- alert -----------------------------------------------------------------


def cmd_alert(args: argparse.Namespace) -> int:
    instance = _resolve_instance(args.instance_dir)
    result = alerts_mod.raise_alert(
        instance,
        title=args.title,
        severity=args.severity,
        body=args.body or "",
        link=args.link or "",
    )
    if result is None:
        raise SystemExit("alert failed (check `jc company status`)")
    print(json.dumps(result, indent=2))
    return 0


# --- approval --------------------------------------------------------------


def cmd_approval(args: argparse.Namespace) -> int:
    instance = _resolve_instance(args.instance_dir)

    payload: dict[str, Any] = {}
    if args.payload:
        payload_path = Path(args.payload).expanduser()
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise SystemExit(f"--payload: {exc}") from exc
        if not isinstance(payload, dict):
            raise SystemExit("--payload JSON must be an object at the top level")

    media_paths = tuple(args.media or ())
    for path in media_paths:
        if not Path(path).exists():
            raise SystemExit(f"--media file not found: {path}")

    result = approvals_mod.raise_approval(
        instance,
        title=args.title,
        type_=args.type,
        payload=payload,
        body=args.body or "",
        media_paths=media_paths,
        expires_in_seconds=args.expires_in,
    )
    if result is None:
        raise SystemExit("approval create failed (check `jc company status`)")

    approval_id = result.get("approval_id") or result.get("id")
    callback_token = result.get("callback_token")
    print(json.dumps(result, indent=2))

    if args.wait and approval_id and callback_token:
        decision = approvals_mod.wait_for_decision(
            instance,
            approval_id=str(approval_id),
            callback_token=str(callback_token),
            timeout=int(args.wait),
        )
        if decision is None:
            print(f"\ntimeout after {args.wait}s; approval still pending", file=sys.stderr)
            return 1
        print("\n--- decision ---")
        print(json.dumps(decision, indent=2))
        return 0 if decision.get("status") == "approved" else 1
    return 0


# --- agents ---------------------------------------------------------------


def cmd_agents_list(args: argparse.Namespace) -> int:
    instance = _resolve_instance(args.instance_dir)
    client = _client_for(instance)
    try:
        result = client.organigram()
    except CompanyError as exc:
        raise SystemExit(f"agents list failed: status={exc.status} {exc.body or exc}") from exc
    finally:
        client.close()
    _print_json(result)
    return 0


# --- task ------------------------------------------------------------------


def cmd_task_inbox(args: argparse.Namespace) -> int:
    instance = _resolve_instance(args.instance_dir)
    client = _client_for(instance)
    try:
        agent = client.whoami()
        agent_id = str(agent.get("id") or "")
        if not agent_id:
            raise SystemExit(f"whoami returned no id: {agent}")
        result = client.get_inbox(
            agent_id=agent_id,
            statuses=tuple(args.status or ("pending", "accepted", "in_progress", "blocked")),
            limit=args.limit,
        )
    except CompanyError as exc:
        raise SystemExit(f"task inbox failed: status={exc.status} {exc.body or exc}") from exc
    finally:
        client.close()
    _print_json(result)
    return 0


def cmd_task_list(args: argparse.Namespace) -> int:
    instance = _resolve_instance(args.instance_dir)
    client = _client_for(instance)
    try:
        result = client.list_tasks(
            statuses=tuple(args.status or ()),
            owner_agent_id=args.owner_agent_id,
            root_id=args.root_id,
            company_id=args.company_id,
            limit=args.limit,
        )
    except CompanyError as exc:
        raise SystemExit(f"task list failed: status={exc.status} {exc.body or exc}") from exc
    finally:
        client.close()
    _print_json(result)
    return 0


def cmd_task_get(args: argparse.Namespace) -> int:
    instance = _resolve_instance(args.instance_dir)
    client = _client_for(instance)
    try:
        result = client.get_task(args.task_id)
    except CompanyError as exc:
        raise SystemExit(f"task get failed: status={exc.status} {exc.body or exc}") from exc
    finally:
        client.close()
    _print_json(result)
    return 0


def _task_body(args: argparse.Namespace, *, include_budgets: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "owner_slug": args.owner,
        "title": args.title,
        "payload": _load_json_arg(args.payload, field="--payload"),
    }
    if args.description:
        body["description"] = args.description
    if getattr(args, "parent_task_id", None):
        body["parent_task_id"] = args.parent_task_id
    if include_budgets:
        for attr in ("max_nodes", "max_depth", "max_age_seconds"):
            value = getattr(args, attr, None)
            if value is not None:
                body[attr] = value
    return body


def cmd_task_create(args: argparse.Namespace) -> int:
    instance = _resolve_instance(args.instance_dir)
    client = _client_for(instance)
    try:
        result = client.create_task(_task_body(args, include_budgets=True))
    except CompanyError as exc:
        raise SystemExit(f"task create failed: status={exc.status} {exc.body or exc}") from exc
    finally:
        client.close()
    _print_json(result)
    return 0


def cmd_task_spawn(args: argparse.Namespace) -> int:
    instance = _resolve_instance(args.instance_dir)
    client = _client_for(instance)
    try:
        result = client.spawn_task(
            args.task_id,
            _task_body(args, include_budgets=False),
        )
    except CompanyError as exc:
        raise SystemExit(f"task spawn failed: status={exc.status} {exc.body or exc}") from exc
    finally:
        client.close()
    _print_json(result)
    return 0


def cmd_task_update(args: argparse.Namespace) -> int:
    instance = _resolve_instance(args.instance_dir)
    body: dict[str, Any] = {}
    if args.status:
        body["status"] = args.status
    if args.result:
        body["result"] = _load_json_arg(args.result, field="--result")
    if not body:
        raise SystemExit("task update requires --status and/or --result")
    client = _client_for(instance)
    try:
        result = client.patch_task(args.task_id, body)
    except CompanyError as exc:
        raise SystemExit(f"task update failed: status={exc.status} {exc.body or exc}") from exc
    finally:
        client.close()
    _print_json(result)
    return 0


# --- replay ----------------------------------------------------------------


def cmd_replay(args: argparse.Namespace) -> int:
    instance = _resolve_instance(args.instance_dir)
    cfg = conf_module.load(instance)
    if not cfg.api_key:
        raise SystemExit("no API key — run `jc company register` first")

    outbox = Outbox(
        instance,
        max_mb=cfg.outbox_max_mb,
        max_age_hours=cfg.outbox_max_age_hours,
    )

    since_mtime: float | None = None
    if args.since:
        try:
            cutoff = _parse_since(args.since)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        since_mtime = cutoff.timestamp()

    client = CompanyClient(cfg)
    try:
        replayed = outbox.drain(
            lambda batch: client.post_events(batch),
            since_mtime=since_mtime,
        )
    finally:
        client.close()
    print(f"replayed {replayed} events")
    return 0


# --- argparse --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jc-company")
    p.add_argument("--instance-dir", help="override instance dir")
    subs = p.add_subparsers(dest="cmd", required=True)

    rp = subs.add_parser("register", help="enroll this instance with The Company")
    rp.add_argument("--endpoint", help="https URL of The Company backend")
    rp.add_argument("--token", help="bootstrap enrollment token")
    rp.add_argument("--name", help="display name (default: IDENTITY.md heading)")

    sp = subs.add_parser("status", help="show local Company state")
    sp.add_argument("--ping", action="store_true", help="also send a heartbeat to verify reachability")

    ap = subs.add_parser("alert", help="raise an alert on The Company")
    ap.add_argument("title")
    ap.add_argument("--severity", default="warn", choices=alerts_mod.SUPPORTED_SEVERITIES)
    ap.add_argument("--body", default=None)
    ap.add_argument("--link", default=None)

    apr = subs.add_parser("approval", help="raise (and optionally wait on) an approval")
    apr.add_argument("title")
    apr.add_argument("--type", default="action", choices=approvals_mod.SUPPORTED_TYPES)
    apr.add_argument("--body", default=None)
    apr.add_argument("--payload", default=None, help="path to JSON payload file")
    apr.add_argument("--media", action="append", default=None, help="path to media artifact (repeatable)")
    apr.add_argument("--expires-in", type=int, default=None, help="seconds until expiry")
    apr.add_argument("--wait", type=int, default=0, help="block up to N seconds waiting for decision")

    ag = subs.add_parser("agents", help="same-company organigram / peer discovery")
    agent_sub = ag.add_subparsers(dest="agents_cmd", required=True)
    agent_sub.add_parser("list", help="list peer agents in this company")

    tp = subs.add_parser("task", help="task graph operations")
    task_sub = tp.add_subparsers(dest="task_cmd", required=True)

    ti = task_sub.add_parser("inbox", help="list this agent's inbox")
    ti.add_argument("--status", action="append", default=None)
    ti.add_argument("--limit", type=int, default=20)

    tl = task_sub.add_parser("list", help="list tasks by status/root/owner id")
    tl.add_argument("--status", action="append", default=None)
    tl.add_argument("--owner-agent-id", default=None)
    tl.add_argument("--root-id", default=None)
    tl.add_argument("--company-id", default=None)
    tl.add_argument("--limit", type=int, default=100)

    tg = task_sub.add_parser("get", help="get task detail")
    tg.add_argument("task_id")

    tc = task_sub.add_parser("create", help="create a root task")
    tc.add_argument("--owner", required=True, help="owner agent slug")
    tc.add_argument("--title", required=True)
    tc.add_argument("--description", default=None)
    tc.add_argument("--payload", default=None, help="inline JSON object or @path")
    tc.add_argument("--parent-task-id", default=None, help="optional parent task id")
    tc.add_argument("--max-nodes", type=int, default=None)
    tc.add_argument("--max-depth", type=int, default=None)
    tc.add_argument("--max-age-seconds", type=int, default=None)

    ts = task_sub.add_parser("spawn", help="spawn a child task under an owned task")
    ts.add_argument("task_id", help="parent task id")
    ts.add_argument("--owner", required=True, help="child owner agent slug")
    ts.add_argument("--title", required=True)
    ts.add_argument("--description", default=None)
    ts.add_argument("--payload", default=None, help="inline JSON object or @path")

    tu = task_sub.add_parser("update", help="patch task status/result")
    tu.add_argument("task_id")
    tu.add_argument("--status", choices=["pending", "accepted", "in_progress", "blocked", "done", "failed", "rejected"])
    tu.add_argument("--result", default=None, help="inline JSON object or @path")

    rpy = subs.add_parser("replay", help="re-send buffered outbox events")
    rpy.add_argument("--since", default=None, help="only replay events newer than this (e.g. 1h, 30m, 2d)")

    return p


HANDLERS = {
    "register": cmd_register,
    "status": cmd_status,
    "alert": cmd_alert,
    "approval": cmd_approval,
    "agents": lambda args: {
        "list": cmd_agents_list,
    }[args.agents_cmd](args),
    "task": lambda args: {
        "inbox": cmd_task_inbox,
        "list": cmd_task_list,
        "get": cmd_task_get,
        "create": cmd_task_create,
        "spawn": cmd_task_spawn,
        "update": cmd_task_update,
    }[args.task_cmd](args),
    "replay": cmd_replay,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return HANDLERS[args.cmd](args)
