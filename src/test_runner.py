from google.adk import Agent, Runner

agent = Agent(
    name="test",
    model="gemini-2.5-flash",
    instruction="say hi"
)

runner = Runner(agent=agent)
for event in runner.run(user_id="1", session_id="1", new_message="hello"):
    print(event)
