"""Agent 市场服务：评测（4.5）与 Fork（4.7）。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Agent, AgentRun, EvalRun, new_id
from . import run_service
from .audit_service import audit


def run_eval(db: Session, agent: Agent, eval_set: list[dict]) -> EvalRun:
    """仿真评测：逐条运行 eval_set，断言 contains_any（输出含任一关键词即过）。

    通过标准（首期）：全部用例通过才 passed=total。结果随 EvalRun 归档。
    """
    results = []
    for case in eval_set:
        run = run_service.run_agent(db, agent, case.get("input", ""), use_rag=True)
        expected_any = case.get("contains_any") or []
        output = run.output_text or ""
        ok = (run.status == "succeeded") and (not expected_any or any(kw in output for kw in expected_any))
        results.append({
            "input": case.get("input", ""),
            "expected_any": expected_any,
            "output": output[:300],
            "pass": ok,
        })
    passed = sum(1 for r in results if r["pass"])
    er = EvalRun(
        id=new_id("eval"),
        agent_id=agent.id,
        agent_version=agent.version,
        total=len(results),
        passed=passed,
        results=results,
        status="done",
    )
    db.add(er)
    audit(db, "agent.eval", target=f"agent:{agent.name}@{agent.version}",
          detail={"passed": passed, "total": len(results)})
    db.flush()
    return er


def fork_agent(db: Session, source: Agent, new_name: str) -> Agent:
    """Fork：复制定义为新草稿（新 name，v1），记录血缘 forked_from。"""
    fork = Agent(
        id=new_id("agent"),
        name=new_name,
        version=1,
        description=f"[fork from {source.name}@{source.version}] {source.description}",
        system_prompt=source.system_prompt,
        model=source.model,
        skill_keys=list(source.skill_keys),
        plugin_keys=dict(source.plugin_keys),
        plugin_config=dict(source.plugin_config),
        workflow=dict(source.workflow),
        eval_set=list(source.eval_set),
        forked_from=f"{source.name}@{source.version}",
        status="published",
    )
    db.add(fork)
    audit(db, "agent.fork", target=f"agent:{new_name}", detail={"source": f"{source.name}@{source.version}"})
    db.flush()
    return fork


def run_playground(db: Session, agent: Agent, input_text: str, on_event=None, history: list[dict] | None = None) -> AgentRun:
    """Playground：所有 Agent 统一走 ReAct 工作循环。

    设计类 Agent（skill-designer / plugin-designer）此前有按名字写死的特判，
    导致任何输入（哪怕是"你是谁"）都被当成设计需求强制执行。已取消：
    它们现在和其它 Agent 一样走普通 ReAct 循环，能正常对话、也能自主调工具设计制品。
    """
    return run_service.run_agent(db, agent, input_text, use_rag=True, on_event=on_event, history=history)
