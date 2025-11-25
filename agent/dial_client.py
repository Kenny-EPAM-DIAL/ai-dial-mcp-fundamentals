import json
from collections import defaultdict
from typing import Any

from openai import AsyncAzureOpenAI

from agent.models.message import Message, Role
from agent.mcp_client import MCPClient


class DialClient:
    """Handles AI model interactions and integrates with MCP client"""

    def __init__(
        self,
        api_key: str,
        endpoint: str,
        tools: list[dict[str, Any]],
        mcp_client: MCPClient,
    ) -> None:
        self.tools = tools
        self.mcp_client = mcp_client
        self.openai = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version="2025-01-01-preview",
        )

    def _collect_tool_calls(self, tool_deltas: list[Any]) -> list[dict[str, Any]]:
        """Convert streaming tool call deltas to complete tool calls"""
        tool_dict: dict[int, dict[str, Any]] = defaultdict(
            lambda: {
                "id": None,
                "function": {"arguments": "", "name": None},
                "type": None,
            }
        )

        for delta in tool_deltas:
            idx = delta.index
            if delta.id:
                tool_dict[idx]["id"] = delta.id
            if delta.function.name:
                tool_dict[idx]["function"]["name"] = delta.function.name
            if delta.function.arguments:
                tool_dict[idx]["function"]["arguments"] += delta.function.arguments
            if delta.type:
                tool_dict[idx]["type"] = delta.type

        return list(tool_dict.values())

    async def _stream_response(self, messages: list[Message]) -> Message:
        """Stream OpenAI response and handle tool calls"""
        stream = await self.openai.chat.completions.create(
            model="gpt-4o",
            messages=[msg.to_dict() for msg in messages],
            tools=self.tools,
            temperature=0.0,
            stream=True,
        )

        content = ""
        tool_deltas: list[Any] = []

        print("🤖: ", end="", flush=True)

        async for chunk in stream:
            delta = chunk.choices[0].delta

            # Stream content tokens
            if delta.content:
                print(delta.content, end="", flush=True)
                content += delta.content

            # Collect tool-call deltas
            if delta.tool_calls:
                tool_deltas.extend(delta.tool_calls)

        print()
        return Message(
            role=Role.AI,
            content=content,
            tool_calls=self._collect_tool_calls(tool_deltas) if tool_deltas else [],
        )

    async def get_completion(self, messages: list[Message]) -> Message:
        """Process user query with streaming and tool calling"""
        ai_message: Message = await self._stream_response(messages)

        # If tools were requested – execute them and recurse
        if ai_message.tool_calls:
            messages.append(ai_message)
            await self._call_tools(ai_message, messages)
            return await self.get_completion(messages)

        return ai_message

    async def _call_tools(self, ai_message: Message, messages: list[Message]) -> None:
        """Execute tool calls using MCP client"""
        for tool_call in ai_message.tool_calls or []:
            tool_name = tool_call["function"]["name"]
            raw_args = tool_call["function"].get("arguments") or "{}"

            try:
                tool_args = json.loads(raw_args)
            except json.JSONDecodeError:
                tool_args = {}

            try:
                result = await self.mcp_client.call_tool(tool_name, tool_args)
                messages.append(
                    Message(
                        role=Role.TOOL,
                        content=str(result),
                        tool_call_id=tool_call["id"],
                    )
                )
            except Exception as exc:  # fallback on tool failure
                error_msg = f"Error during `{tool_name}` call: {exc}"
                print(error_msg)
                messages.append(
                    Message(
                        role=Role.TOOL,
                        content=error_msg,
                        tool_call_id=tool_call["id"],
                    )
                )
