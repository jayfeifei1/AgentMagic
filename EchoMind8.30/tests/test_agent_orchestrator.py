import asyncio

from agents.agent_orchestrator import (
    AgentProfile,
    AgentResponse,
    AgentOrchestrator,
    AgentType,
    BillingAgent,
    EscalationAgent,
    GeneralAgent,
    OrchestratorResult,
    Request,
    ResponseComposer,
    RoutingDecision,
    TechnicalAgent,
    build_shared_rag_tools,
)
from core.intent_recognizer import IntentCategory, UrgencyLevel


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

        class Messages:
            async def create(inner, **kwargs):
                self.calls.append(kwargs)
                if self.error:
                    raise self.error
                return self.response

        self.messages = Messages()


def make_request(**kwargs):
    values = {
        "message": "登录时报 401，同时这笔订单被重复扣款",
        "user_id": "u1",
        "conv_id": "c1",
        "intent": IntentCategory.TECHNICAL_LOGIN,
        "intent_group": "technical",
        "urgency": UrgencyLevel.HIGH,
        "intent_confidence": 0.92,
        "entities": {"error_code": ["401"], "amount": ["99 元"]},
    }
    values.update(kwargs)
    return Request(**values)


def test_agent_profiles_have_distinct_contracts_and_generation_config():
    assert isinstance(GeneralAgent.profile, AgentProfile)
    assert GeneralAgent.profile.role != TechnicalAgent.profile.role
    assert TechnicalAgent.profile.workflow != BillingAgent.profile.workflow
    assert TechnicalAgent.profile.temperature < GeneralAgent.profile.temperature
    assert "search_knowledge_base" in GeneralAgent.profile.tool_scope
    assert "lookup_error_code" in TechnicalAgent.profile.tool_scope
    assert "check_billing_fields" in BillingAgent.profile.tool_scope


def test_domain_agents_build_different_role_packets():
    req = make_request()
    general_packet = GeneralAgent(FakeClient(), "test-model")._build_role_packet(req)
    technical_packet = TechnicalAgent(FakeClient(), "test-model")._build_role_packet(req)
    billing_packet = BillingAgent(FakeClient(), "test-model")._build_role_packet(req)

    assert "triage_targets" in general_packet
    assert "diagnostic_fields" in technical_packet
    assert "verification_fields" in billing_packet
    assert general_packet != technical_packet != billing_packet


def test_escalation_agent_is_a_real_non_llm_handoff_node():
    client = FakeClient()
    agent = EscalationAgent(client, "test-model")

    result = asyncio.run(agent.handle(make_request(
        intent=IntentCategory.HUMAN_HANDOFF,
        urgency=UrgencyLevel.CRITICAL,
    )))

    assert result.success is True
    assert result.escalate is True
    assert "人工升级" in result.content
    assert client.calls == []


def test_composer_fallback_preserves_primary_and_supporting_results():
    composer = ResponseComposer(FakeClient(error=RuntimeError("provider down")), "test-model")
    req = make_request()
    responses = [
        AgentResponse(AgentType.TECHNICAL, "先排查 Token 是否过期。", True),
        AgentResponse(AgentType.BILLING, "请提供两笔扣款的时间和金额。", True),
    ]

    composition = asyncio.run(composer.compose(req, responses))
    content = composition.content

    assert content.startswith("先排查 Token 是否过期。")
    assert "补充说明" in content
    assert "两笔扣款" in content
    assert composition.llm_traces[0]["agent_type"] == "composer"
    assert composition.llm_traces[0]["status"] == "failed"


def test_composer_records_successful_llm_call():
    class TextBlock:
        type = "text"
        text = "已合并技术与账单处理建议。"

    composer = ResponseComposer(
        FakeClient(response=type("Response", (), {"content": [TextBlock()]})()),
        "test-model",
    )
    result = asyncio.run(composer.compose(
        make_request(),
        [
            AgentResponse(AgentType.TECHNICAL, "技术建议", True),
            AgentResponse(AgentType.BILLING, "账单建议", True),
        ],
    ))

    assert result.content == "已合并技术与账单处理建议。"
    assert result.llm_traces[0]["agent_type"] == "composer"
    assert result.llm_traces[0]["status"] == "success"


def test_routing_decision_can_target_escalation_pool():
    # Keep this assertion close to the public data contract used by the API.
    decision = RoutingDecision(
        primary_agent=AgentType.ESCALATION,
        reason="critical request",
        confidence=1.0,
    )
    assert decision.agent_types == [AgentType.ESCALATION]
    assert not decision.multi_agent


def test_agent_tool_scopes_are_real_and_isolated():
    general_tools = set(GeneralAgent(FakeClient(), "test-model").get_tools())
    technical_tools = set(TechnicalAgent(FakeClient(), "test-model").get_tools())
    billing_tools = set(BillingAgent(FakeClient(), "test-model").get_tools())
    escalation_tools = set(EscalationAgent(FakeClient(), "test-model").get_tools())

    assert general_tools == {"inspect_request_context", "suggest_required_fields", "request_human_handoff"}
    assert technical_tools == {"lookup_error_code", "build_diagnostic_plan", "request_human_handoff"}
    assert billing_tools == {"check_billing_fields", "compare_amounts", "request_human_handoff"}
    assert escalation_tools == {"create_handoff_summary"}
    assert general_tools & technical_tools == {"request_human_handoff"}
    assert technical_tools & billing_tools == {"request_human_handoff"}


def test_shared_rag_tool_is_available_to_all_agents():
    class RagManager:
        async def search_with_rewrite(self, tool_name, query, top_k=5):
            return type(
                "Result",
                (),
                {"success": True, "data": [{"title": "退款政策", "content": "7 天内可退款"}], "reranked": True},
            )()

    shared = build_shared_rag_tools(RagManager())

    general = GeneralAgent(FakeClient(), "test-model")
    technical = TechnicalAgent(FakeClient(), "test-model")
    billing = BillingAgent(FakeClient(), "test-model")
    escalation = EscalationAgent(FakeClient(), "test-model")

    for agent in (general, technical, billing, escalation):
        agent.set_shared_tools(shared)
        tools = agent.get_tools()
        assert "search_knowledge_base" in tools


def test_tool_input_validation_rejects_unknown_fields():
    agent = TechnicalAgent(FakeClient(), "test-model")
    spec = agent.get_tools()["lookup_error_code"]

    try:
        agent._validate_tool_input(spec, {"error_code": "401", "secret": "nope"})
    except ValueError as exc:
        assert "不允许的工具参数" in str(exc)
    else:
        raise AssertionError("unknown tool fields should be rejected")


def test_tool_use_round_trip_executes_only_whitelisted_tool():
    class ToolUseBlock:
        type = "tool_use"
        id = "toolu_1"
        name = "lookup_error_code"
        input = {"error_code": "401"}

    class TextBlock:
        type = "text"
        text = "已根据 401 错误码给出排查建议。"

    class ToolClient:
        def __init__(self):
            self.calls = []
            self.responses = [
                type("Response", (), {"content": [ToolUseBlock()]})(),
                type("Response", (), {"content": [TextBlock()]})(),
            ]

        class Messages:
            def __init__(self, owner):
                self.owner = owner

            async def create(self, **kwargs):
                self.owner.calls.append(kwargs)
                return self.owner.responses.pop(0)

        @property
        def messages(self):
            return self.Messages(self)

    client = ToolClient()
    agent = TechnicalAgent(client, "test-model")
    response = asyncio.run(agent.handle(make_request()))

    assert response.success is True
    assert response.tools_used == ["lookup_error_code"]
    assert len(response.tool_traces) == 1
    assert response.tool_traces[0]["tool_name"] == "lookup_error_code"
    assert response.tool_traces[0]["input"] == {"error_code": "401"}
    assert response.tool_traces[0]["success"] is True
    assert [item["round_no"] for item in response.llm_traces] == [1, 2]
    assert all(item["model"] == "test-model" for item in response.llm_traces)
    assert all(item["status"] == "success" for item in response.llm_traces)
    assert len(client.calls) == 2
    assert {tool["name"] for tool in client.calls[0]["tools"]} == {
        "lookup_error_code",
        "build_diagnostic_plan",
        "request_human_handoff",
    }
    assert "tool_result" in str(client.calls[1]["messages"])


def test_textual_handoff_hint_does_not_mark_response_escalated():
    class TextBlock:
        type = "text"
        text = "请先检查 Token；若仍无法解决，可以建议转人工继续排查。"

    agent = TechnicalAgent(
        FakeClient(response=type("Response", (), {"content": [TextBlock()]})()),
        "test-model",
    )

    response = asyncio.run(agent.handle(make_request(message="登录提示 401，请给出排查步骤。")))

    assert response.success is True
    assert response.escalate is False


def test_handoff_function_call_marks_response_escalated_and_records_trace():
    class ToolUseBlock:
        type = "tool_use"
        id = "toolu_handoff"
        name = "request_human_handoff"
        input = {"reason": "需要后台日志进一步核验", "priority": "high"}

    class TextBlock:
        type = "text"
        text = "已记录人工介入申请，请等待后续处理。"

    class ToolClient:
        def __init__(self):
            self.responses = [
                type("Response", (), {"content": [ToolUseBlock()]})(),
                type("Response", (), {"content": [TextBlock()]})(),
            ]

        class Messages:
            def __init__(self, owner):
                self.owner = owner

            async def create(self, **kwargs):
                return self.owner.responses.pop(0)

        @property
        def messages(self):
            return self.Messages(self)

    response = asyncio.run(TechnicalAgent(ToolClient(), "test-model").handle(make_request()))

    assert response.success is True
    assert response.escalate is True
    assert response.tools_used == ["request_human_handoff"]
    assert response.tool_traces[0]["tool_name"] == "request_human_handoff"
    assert response.tool_traces[0]["input"]["reason"] == "需要后台日志进一步核验"


def test_concurrent_requests_keep_agent_traces_isolated():
    class ToolUseBlock:
        type = "tool_use"

        def __init__(self, marker):
            self.id = f"toolu_{marker}"
            self.name = "lookup_error_code"
            self.input = {"error_code": marker}

    class TextBlock:
        type = "text"

        def __init__(self, marker):
            self.text = f"{marker} 的排查建议"

    class ConcurrentClient:
        class Messages:
            async def create(inner, **kwargs):
                await asyncio.sleep(0)
                payload = str(kwargs["messages"])
                marker = "401" if "401" in payload else "500"
                if "tool_result" in payload:
                    return type("Response", (), {"content": [TextBlock(marker)]})()
                return type("Response", (), {"content": [ToolUseBlock(marker)]})()

        messages = Messages()

    async def run_both():
        agent = TechnicalAgent(ConcurrentClient(), "test-model")
        return await asyncio.gather(
            agent.handle(make_request(message="登录报 401", entities={"error_code": ["401"]})),
            agent.handle(make_request(message="服务报 500", entities={"error_code": ["500"]})),
        )

    first, second = asyncio.run(run_both())
    assert first.tool_traces[0]["input"] == {"error_code": "401"}
    assert second.tool_traces[0]["input"] == {"error_code": "500"}
    assert len(first.llm_traces) == len(second.llm_traces) == 2


def test_orchestrator_persists_complete_trace_through_repository():
    class RecordingRepository:
        def __init__(self):
            self.traces = []

        async def persist(self, trace):
            self.traces.append(trace)
            return True

    repository = RecordingRepository()
    orchestrator = AgentOrchestrator(api_key="test-key", model="test-model", trace_repository=repository)
    result = OrchestratorResult(
        request_id="trace-unit-check",
        response="已完成排查。",
        agent_type=AgentType.TECHNICAL,
        intent=IntentCategory.TECHNICAL_LOGIN,
        primary_agent=AgentType.TECHNICAL,
        tools_used=["lookup_error_code"],
        tool_traces=[{"tool_name": "lookup_error_code"}],
        llm_traces=[{"agent_type": "technical", "round_no": 1, "model": "test-model"}],
        routing_reason="unit test",
        routing_confidence=0.9,
    )

    asyncio.run(orchestrator._record_tool_trace(result))

    assert repository.traces[0]["request_id"] == "trace-unit-check"
    assert repository.traces[0]["tool_calls"][0]["tool_name"] == "lookup_error_code"
    assert repository.traces[0]["llm_calls"][0]["round_no"] == 1
