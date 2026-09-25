"""End-to-end demo with no API keys and no network: `uv run python examples/offline_demo.py`.

1. a normal tool-using run
2. an indirect prompt-injection attempt stopped at the approval gate
3. a destructive request that waits for a human, then runs exactly once
"""

from __future__ import annotations

import asyncio
import tempfile

from agentplat.approvals import decide
from agentplat.config import Settings
from agentplat.container import build_container
from agentplat.store.models import Agent
from agentplat.store.sql import new_id


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        c = build_container(
            Settings(
                database_url=f"sqlite+aiosqlite:///{tmp}/demo.db",
                embedded_worker=False,
                offline_web=True,
                _env_file=None,
            )
        )
        await c.store.create_schema()
        await c.store.grant("acme", "alice", ["web:read", "notes:write", "records:delete"])
        await c.store.grant("acme", "boss", ["approvals:decide"])
        agent = Agent(new_id(), "acme", "demo", "You are helpful.", [t.name for t in c.registry])
        await c.store.create_agent(agent)

        async def ask(text: str) -> str:
            run = await c.runtime.execute((await c.runtime.start_run(agent, "alice", text)).id)
            print(f"\n> {text}\n  status={run.status.value} tainted={run.tainted}")
            print(f"  answer: {run.final_output!r}")
            return run.id

        print("1) normal run")
        await ask("What's 17% of 2,340 plus 12?")

        print("\n2) page with hidden instructions to write user data to memory")
        await ask("Summarise http://blog.test/injected")
        for a in await c.store.list_approvals("acme", "pending"):
            print(f"  pending approval: {a.tool_name} {a.args} - reason: {a.reason}")
        print(f"  exfil note written? {await c.store.get('acme', 'alice', 'exfil') is not None}")

        print("\n3) destructive request")
        run_id = await ask("Delete all orders")
        (approval,) = [
            a for a in await c.store.list_approvals("acme", "pending") if a.run_id == run_id
        ]
        print(f"  orders before approval: {len(c.services.records.tables['orders'])}")
        await decide(
            c, tenant_id="acme", user_id="boss", approval_id=approval.id, decision="approve"
        )
        await c.worker().process_one()
        run = await c.store.get_run(run_id)
        print(
            f"  after approval: status={run.status.value}, "
            f"orders left={len(c.services.records.tables['orders'])}"
        )

        spans = await c.store.list_spans(run_id)
        print(f"\ntrace for run 3: {[s['name'] for s in spans]}")
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
