# README artwork

The hero identifies the products AgentPeers connects. It depicts two separate pairs, not an endorsement or a connection between every product shown.

| Asset | Source |
| --- | --- |
| `grokbot.png` | Official Grok Bot app icon, version 0.75.0. [Product](https://x.ai/bot) · [Brand guidelines](https://x.ai/legal/brand-guidelines). |
| `dot.svg` | Dot artwork linked by OpenAI's [Introducing dots](https://openai.com/index/introducing-dots/) announcement. [Original SVG](https://images.ctfassets.net/kftzwdyauwt9/KKSjto3MtJguvsLQsM1Bp/1704a6937d22b1bb3cac38dc5f43f9df/dots-o.svg). |
| `codex.png` | Official Codex app icon, version 26.1016.62342, proportionally scaled for the README. [Product](https://openai.com/codex/) · [Brand guidelines](https://openai.com/brand/). |
| `claude-code.svg` | Asterisk mark from the [official Claude Code documentation](https://code.claude.com/docs/en/overview) SVG header. Original path and color retained. [Source SVG](https://mintcdn.com/claude-code/c5r9_6tjPMzFdDDT/logo/light.svg). |

Product names and marks remain the property of their respective owners and are not covered by this repository's MIT license. AgentPeers is an independent integration.

`agentpeers-hero.svg` is the editable composition, including embedded copies of the icons so it has no external image or font dependencies. `agentpeers-hero.png` is the GitHub display export. To regenerate it with the optional librsvg command-line tool:

```sh
rsvg-convert docs/assets/agentpeers-hero.svg -o docs/assets/agentpeers-hero.png
```

The artwork tools are not dependencies of AgentPeers.
