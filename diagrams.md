# Diagrams

## 1. Flow: from question to answer

```mermaid
flowchart TD
    Q["User asks a question"] --> A["Agent + Gemini<br/>pick a tool"]
    A --> T["Tool runs<br/>(list, search, read, send)"]
    T --> G["Microsoft Graph<br/>returns emails"]
    G --> A2["Gemini writes the answer"]
    A2 --> R["User sees the answer"]

    T -.->|"send / reply only"| C{"User approves?"}
    C -- yes --> G
    C -- no --> X["Nothing is sent"]
```

Sending has two checks: the agent shows the draft and waits for "yes", then ADK asks you to approve the exact email before it goes out.

## 2. Components (current)

```mermaid
flowchart LR
    U["User<br/>adk web / adk run"] --> AG["agent.py<br/>instructions + model"]
    AG <--> GEM["Gemini API"]
    AG --> TL["tools.py"]
    TL --> GC["graph_client.py"]
    GC --> GR["Microsoft Graph"]
    GC --> AU["auth.py"]
    AU <--> TC[("token_cache.json")]
    AU <--> MS["Microsoft sign-in"]
```

- `agent.py`: what the model is told, and which tools it may use.
- `tools.py`: the actions the model can take.
- `graph_client.py`: the only code that calls Microsoft Graph.
- `auth.py`: signs you in once (`mail-login`) and keeps the token fresh.
