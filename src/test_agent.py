import asyncio
from google.adk import Agent

agent = Agent(
    name="test",
    model="gemini-2.5-flash",
    instruction="say hi"
)

async def test():
    async for event in agent.run(node_input="hi"):
        print(event)

if __name__ == "__main__":
    asyncio.run(test())
