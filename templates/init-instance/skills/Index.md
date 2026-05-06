# Skill Index

This directory contains instance-owned agent skills. Read this index before
choosing a web, search, crawl, or browser automation skill.

| Skill | Path | Credential | Use for |
|-------|------|------------|---------|
| Brave Search | `skills/brave/SKILL.md` | `BRAVE_API_KEY` | Provider-diverse web search from Brave's independent index |
| Tavily | `skills/tavily/SKILL.md` | `TAVILY_API_KEY` | Agent-optimized search, extraction, crawl, map, and research |
| Firecrawl | `skills/firecrawl/SKILL.md` | `FIRECRAWL_API_KEY` | Clean page/site extraction, crawling, structured scrape, JS-heavy pages |
| Browser Use | `skills/browser-use/SKILL.md` | `BROWSER_USE_API_KEY` | Interactive browser automation when APIs or scraping are insufficient |

Credentials live in the instance root `.env`. Never put API keys in skill
files, prompts, memory, logs, or committed examples.

When adding a new instance skill, create `skills/<skillname>/SKILL.md` and add a
row here.
