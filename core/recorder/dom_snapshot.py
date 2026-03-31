from __future__ import annotations

import json

from playwright.async_api import Page


class DomSnapshotter:
    async def capture(self, page: Page) -> str:
        """Capture a compact JSON snapshot of the current page's interactive elements."""
        snapshot = await page.evaluate("""() => {
            const getInteractive = () => {
                const elements = [];
                document.querySelectorAll('button, [role="button"], input, select, textarea, a[href], [data-testid]').forEach(el => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return;
                    elements.push({
                        tag: el.tagName.toLowerCase(),
                        type: el.type || null,
                        text: (el.textContent || el.value || el.placeholder || '').trim().slice(0, 80),
                        name: el.name || null,
                        id: el.id || null,
                        ariaLabel: el.getAttribute('aria-label'),
                        href: el.href || null,
                    });
                });
                return elements.slice(0, 30);
            };
            return {
                url: window.location.href,
                title: document.title,
                elements: getInteractive(),
                forms: Array.from(document.forms).map(f => ({
                    id: f.id,
                    action: f.action,
                    method: f.method,
                    fieldNames: Array.from(f.elements).map(e => e.name).filter(Boolean),
                })),
            };
        }""")
        return json.dumps(snapshot)
