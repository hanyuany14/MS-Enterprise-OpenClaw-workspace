"""
Auto-Approve Agent Versions
針對 Azure AI Foundry Portal 上的 Agent，自動建立指定 require_approval 的新版本。
"""

import os
import sys
import asyncio
from azure.identity.aio import DefaultAzureCredential
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition


VALID_MODES = ("never", "always")


async def create_version_with_approval(
    project_client: AIProjectClient,
    agent_name: str,
    approval_mode: str,
) -> str:
    agent_def = await project_client.agents.get(agent_name=agent_name)
    latest = agent_def.versions.latest
    definition = latest.definition

    print(f"  [{agent_name}] Latest version: {latest.version}")

    mcp_tools = [t for t in definition.get("tools", []) if t.get("type") == "mcp"]

    if not mcp_tools:
        print(f"  [{agent_name}] ⚠ No MCP tools found, skipping")
        return latest.version

    # 檢查是否已經是目標 mode
    all_match = all(t.get("require_approval") == approval_mode for t in mcp_tools)
    if all_match:
        print(f"  [{agent_name}] ✓ Already require_approval={approval_mode} (v{latest.version})")
        return latest.version

    # 建立新版本
    modified_tools = []
    for tool in definition.get("tools", []):
        if tool.get("type") == "mcp":
            new_tool = {
                "type": "mcp",
                "server_label": tool.get("server_label"),
                "server_url": tool.get("server_url"),
                "require_approval": approval_mode,
            }
            for key in ("allowed_tools", "project_connection_id"):
                if tool.get(key):
                    new_tool[key] = tool[key]
            modified_tools.append(new_tool)
        else:
            modified_tools.append(tool)

    new_agent = await project_client.agents.create_version(
        agent_name=agent_name,
        definition=PromptAgentDefinition(
            model=definition.get("model"),
            instructions=definition.get("instructions"),
            tools=modified_tools,
        ),
        description=f"require_approval={approval_mode} (based on v{latest.version})",
    )

    print(f"  [{agent_name}] ✓ Created version: {new_agent.version} (require_approval={approval_mode})")
    return new_agent.version


async def main():
    endpoint = "https://stephen-ai-foundry-swed-resource.services.ai.azure.com/api/projects/stephen_ai_foundry_sweden"

    # --- 使用者輸入 ---
    print(f"Supported approval modes: {', '.join(VALID_MODES)}")
    mode = input("Enter require_approval mode [never]: ").strip().lower() or "never"
    if mode not in VALID_MODES:
        print(f"❌ Invalid mode '{mode}'. Must be one of: {', '.join(VALID_MODES)}")
        sys.exit(1)

    credential = DefaultAzureCredential()
    project_client = AIProjectClient(endpoint=endpoint, credential=credential)

    try:
        print(f"\nEndpoint: {endpoint}")
        print(f"Mode: require_approval={mode}\n")
        version = await create_version_with_approval(project_client, "MemoryFollowUpAgent1", mode)
        print(f"  → Ready: v{version}\n")
    finally:
        await project_client.close()
        await credential.close()


if __name__ == "__main__":
    asyncio.run(main())