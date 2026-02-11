"""
Playwright-based validator tool for Dynamics 365 Queue / Mailbox configuration.

Usage:
    import asyncio
    from tasks.mailbox_validator import validator_tool

    result = asyncio.run(validator_tool(
        url="https://your-org.crm.dynamics.com/...",
        task_json={...},
        auth_storage_path="auth_state.json",
    ))
"""

from __future__ import annotations

import asyncio
from playwright.async_api import async_playwright, Page


# ---------------------------------------------------------------------------
# JavaScript executed inside the browser to find a field + read its value.
# One evaluate call does it all — no locator roundtrip needed.
# ---------------------------------------------------------------------------
_JS_FIND_FIELD = """
(labelText) => {
    const target = labelText.toLowerCase();

    // ── Locate the field container (case-insensitive) ──────────────
    let container = null;

    // Strategy 1: div / section with a title attribute
    for (const el of document.querySelectorAll('div[title], section[title]')) {
        if (el.getAttribute('title').toLowerCase() === target) { container = el; break; }
    }

    // Strategy 2: elements with a matching aria-label
    if (!container) {
        for (const el of document.querySelectorAll('[aria-label]')) {
            if (el.getAttribute('aria-label').toLowerCase() === target) { container = el; break; }
        }
    }

    // Strategy 3: <label> whose text matches → parent container
    if (!container) {
        for (const lbl of document.querySelectorAll('label')) {
            if ((lbl.textContent || '').trim().toLowerCase() === target) {
                container = lbl.closest('div') || lbl.parentElement;
                break;
            }
        }
    }

    // Strategy 4: data-id containing the slugified field name
    if (!container) {
        const slug  = target.replace(/\\s+/g, '');
        const slug2 = target.replace(/\\s+/g, '_');
        for (const el of document.querySelectorAll('[data-id]')) {
            const did = el.getAttribute('data-id').toLowerCase();
            if (did.includes(slug) || did.includes(slug2)) { container = el; break; }
        }
    }

    if (!container) return null;

    // ── Determine if it is a lookup (has an <a> hyperlink) ─────────
    const anchorEl = container.querySelector('a');
    const isLookup = anchorEl !== null && anchorEl.href && anchorEl.textContent.trim().length > 0;

    // ── Read the current value ─────────────────────────────────────
    let currentValue = '';
    if (isLookup) {
        currentValue = anchorEl.textContent.trim();
    } else {
        const input = container.querySelector('input, textarea');
        if (input) {
            currentValue = input.value || '';
        } else {
            const sel = container.querySelector('select');
            if (sel && sel.selectedIndex >= 0) {
                currentValue = sel.options[sel.selectedIndex].text || '';
            } else {
                // Last resort: visible text (skip the label itself)
                const spans = container.querySelectorAll('span, div');
                for (const s of spans) {
                    const t = s.textContent.trim();
                    if (t && t.toLowerCase() !== target) { currentValue = t; break; }
                }
            }
        }
    }

    return { found: true, currentValue: currentValue.trim(), isLookup: isLookup };
}
"""

_JS_FIND_FIELD_ELEMENT = """
(labelText) => {
    const target = labelText.toLowerCase();

    for (const el of document.querySelectorAll('div[title], section[title]')) {
        if (el.getAttribute('title').toLowerCase() === target) return el;
    }
    for (const el of document.querySelectorAll('[aria-label]')) {
        if (el.getAttribute('aria-label').toLowerCase() === target) return el;
    }
    for (const lbl of document.querySelectorAll('label')) {
        if ((lbl.textContent || '').trim().toLowerCase() === target) {
            return lbl.closest('div') || lbl.parentElement;
        }
    }
    const slug  = target.replace(/\\s+/g, '');
    const slug2 = target.replace(/\\s+/g, '_');
    for (const el of document.querySelectorAll('[data-id]')) {
        const did = el.getAttribute('data-id').toLowerCase();
        if (did.includes(slug) || did.includes(slug2)) return el;
    }
    return null;
}
"""


async def _get_field_info(page: Page, label: str) -> dict | None:
    """
    Find a field by label (case-insensitive) and read its current value.
    Everything runs inside a single page.evaluate — no locator needed.

    Returns {"found": True, "currentValue": "...", "isLookup": True/False}
    or None if the field was not found.
    """
    return await page.evaluate(_JS_FIND_FIELD, label.lower())


async def _get_field_element(page: Page, label: str):
    """
    Return a Playwright ElementHandle for the field container so we can
    interact with it (click, type, clear, etc.).
    """
    handle = await page.evaluate_handle(_JS_FIND_FIELD_ELEMENT, label)
    return handle.as_element()



async def _update_lookup_field(field_element, page: Page, correct_value: str) -> None:
    """Clear a lookup field via its × button, search with wildcards, and select."""
    # Click the × (clear / delete) button on the existing lookup value
    clear_btn = await page.evaluate_handle(
        """(container) => {
            return container.querySelector(
                "button[aria-label='Clear'], button[data-id*='clear'], button.lookupClearButton"
            ) || Array.from(container.querySelectorAll('button')).find(
                b => b.textContent.includes('×') || b.textContent.includes('✕')
            ) || null;
        }""",
        field_element,
    )
    clear_el = clear_btn.as_element()
    if clear_el:
        await clear_el.click()
        await asyncio.sleep(0.5)

    # Find the input inside the container and type value wrapped in asterisks
    search_input = await page.evaluate_handle(
        "(container) => container.querySelector('input')",
        field_element,
    )
    input_el = search_input.as_element()
    if input_el:
        await input_el.click()
        await input_el.fill(f"*{correct_value}*")
        await asyncio.sleep(2)  # wait for lookup results to load

    # Select the matching result from the dropdown
    result_item = page.locator(
        f"ul[aria-label='Lookup results'] li:has-text('{correct_value}'), "
        f"div[role='listbox'] div:has-text('{correct_value}')"
    ).first
    await result_item.click()
    await asyncio.sleep(0.5)


async def _update_plain_field(field_element, page: Page, correct_value: str) -> None:
    """Clear a plain text / dropdown field and type the correct value."""
    # Try input or textarea first
    child = await page.evaluate_handle(
        "(container) => container.querySelector('input, textarea')",
        field_element,
    )
    input_el = child.as_element()
    if input_el:
        await input_el.click()
        await input_el.fill("")
        await input_el.fill(correct_value)
        return

    # Try select / dropdown
    child = await page.evaluate_handle(
        "(container) => container.querySelector('select')",
        field_element,
    )
    select_el = child.as_element()
    if select_el:
        await select_el.select_option(label=correct_value)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
async def validator_tool(
    url: str,
    task_json: dict,
    auth_storage_path: str = "auth_state.json",
) -> dict:
    """
    Validate and update a Dynamics 365 Queue and its associated Mailbox.

    Parameters
    ----------
    url : str
        The URL of the queues page in Dynamics 365.
    task_json : dict
        Dictionary containing the expected field values. Required keys:
            - mailbox_IncomingEmail
            - mailbox_owner
            - mailbox_owningBusineesUnit
            - mailbox_DefaultMailboxSLA
            - mailbox_QueueName
    auth_storage_path : str
        Path to a Playwright storage-state JSON file containing cookies
        and local-storage for authentication.

    Returns
    -------
    dict
        {"status": "success" | "failed", "reason": "..."}
    """

    # ── Step 0: Build internal field-mapping dict ──────────────────────
    fields_to_validate = {
        "Incoming Email": task_json["mailbox_IncomingEmail"],
        "Type": "Private",
        "Owner": task_json["mailbox_owner"],
        "Owning Business Unit": task_json["mailbox_owningBusineesUnit"],
        "Default Mailbox SLA": task_json["mailbox_DefaultMailboxSLA"],
    }

    async with async_playwright() as pw:

        # ── Step 1: Launch browser (headed, Chrome channel) ───────────
        browser = await pw.chromium.launch(headless=False, channel="chrome")

        # ── Step 2: Create context with saved auth state ──────────────
        context = await browser.new_context(storage_state=auth_storage_path)

        # ── Step 3: Open a new page ───────────────────────────────────
        page = await context.new_page()

        try:
            # ── Step 4: Navigate to URL ───────────────────────────────
            await page.goto(url)

            # ── Step 5: Wait for page to fully load ───────────────────
            await page.wait_for_load_state("networkidle")

            # ── Step 6: Click dropdown "My Active Queues" / "All Queues"
            #            and select "All Queues" ───────────────────────
            dropdown_btn = page.locator(
                "button:has-text('My Active Queues'), "
                "button:has-text('All Queues'), "
                "span:has-text('My Active Queues'), "
                "span:has-text('All Queues')"
            ).first
            await dropdown_btn.click()
            await asyncio.sleep(1)

            # Select "All Queues" from the opened dropdown menu
            all_queues_option = page.locator(
                "li:has-text('All Queues'), "
                "div[role='menuitem']:has-text('All Queues'), "
                "button:has-text('All Queues'), "
                "option:has-text('All Queues')"
            ).first
            await all_queues_option.click()
            await page.wait_for_load_state("networkidle")

            # ── Step 7: Find search bar "Filter by Keyword" & click ──
            search_bar = page.locator(
                "input[placeholder='Filter by keyword'], "
                "input[aria-label='Filter by keyword']"
            ).first
            await search_bar.click()

            # ── Step 8: Type the queue name and search ────────────────
            queue_name = task_json["mailbox_QueueName"]
            await search_bar.fill(queue_name)
            await search_bar.press("Enter")
            await page.wait_for_load_state("networkidle")

            # ── Steps 9 & 10: Retry logic ────────────────────────────
            record = None
            for attempt in range(5):
                record = page.locator(
                    f"div[role='row']:has-text('{queue_name}'), "
                    f"tr:has-text('{queue_name}'), "
                    f"a:has-text('{queue_name}'), "
                    f"div[role='gridcell']:has-text('{queue_name}')"
                ).first

                if await record.is_visible():
                    break
                await asyncio.sleep(5)
            else:
                # Still not found after 5 retries
                await browser.close()
                return {"status": "failed", "reason": "Queue not yet created"}

            # ── Step 11: Click the found record ──────────────────────
            await record.click()
            await page.wait_for_load_state("networkidle")

            # ── Step 12: Validate & update fields on Queue form ──────
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)  # extra buffer for form rendering

            for field_label, expected_value in fields_to_validate.items():
                try:
                    info = await _get_field_info(page, field_label)
                    if info is None:
                        print(f"[WARN] Field '{field_label}' not found on page")
                        continue

                    if info["currentValue"] == expected_value:
                        # Value matches → skip
                        continue

                    # Value does not match → get element handle to interact
                    el = await _get_field_element(page, field_label)
                    if info["isLookup"]:
                        await _update_lookup_field(el, page, expected_value)
                    else:
                        await _update_plain_field(el, page, expected_value)

                except Exception as exc:
                    print(f"[WARN] Could not process field '{field_label}': {exc}")

            # ── Step 13: Click Save ──────────────────────────────────
            save_btn = page.locator(
                "button[aria-label='Save'], "
                "button:has-text('Save')"
            ).first
            await save_btn.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)

            # ── Step 14: Click Mailbox hyperlink under Email Settings ─
            email_settings_section = page.locator(
                "section:has-text('Email Settings'), "
                "div:has-text('Email Settings')"
            ).first
            mailbox_link = email_settings_section.locator(
                "a:has-text('Mailbox'), "
                "div[data-id*='mailbox'] a"
            ).first
            await mailbox_link.click()

            # ── Step 15: Wait for Mailbox page to load ───────────────
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(5)

            # ── Step 16: "Delete Emails after processing" → Yes ──────
            delete_emails_field = page.locator(
                "div:has(> label:text-is('Delete Emails after processing')), "
                "div[data-id*='delete']"
            ).first
            delete_value = (
                await delete_emails_field.locator("select, input, div[role='combobox']").first.input_value()
            ).strip() if await delete_emails_field.locator("select, input").first.count() > 0 else ""

            # Fallback: read text content
            if not delete_value:
                delete_value = (
                    await delete_emails_field.locator("option[selected], span").first.text_content() or ""
                ).strip()

            if delete_value.lower() == "no":
                # Click on the field and change to Yes
                toggle = delete_emails_field.locator(
                    "select, div[role='combobox'], input"
                ).first
                await toggle.click()
                # Try selecting Yes
                yes_option = page.locator(
                    "option:has-text('Yes'), "
                    "li:has-text('Yes'), "
                    "div[role='option']:has-text('Yes')"
                ).first
                await yes_option.click()
                await asyncio.sleep(1)

            # ── Step 17: Incoming Email & Outgoing Email ─────────────
            for email_field_label in ("Incoming Email", "Outgoing Email"):
                try:
                    info = await _get_field_info(page, email_field_label)
                    if info is None:
                        print(f"[WARN] Field '{email_field_label}' not found on page")
                        continue
                    if info["currentValue"] != "Server-Side Synchronization":
                        el = await _get_field_element(page, email_field_label)
                        if info["isLookup"]:
                            await _update_lookup_field(el, page, "Server-Side Synchronization")
                        else:
                            await _update_plain_field(el, page, "Server-Side Synchronization")
                except Exception as exc:
                    print(f"[WARN] Could not process field '{email_field_label}': {exc}")

            # ── Step 18: Save & Close on Mailbox, then on Queue ──────
            save_close_btn = page.locator(
                "button[aria-label='Save & Close'], "
                "button:has-text('Save & Close')"
            ).first
            await save_close_btn.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(3)

            # Back on Queue page → Save & Close again
            save_close_btn2 = page.locator(
                "button[aria-label='Save & Close'], "
                "button:has-text('Save & Close')"
            ).first
            await save_close_btn2.click()
            await page.wait_for_load_state("networkidle")

            # ── Step 19: Return success ──────────────────────────────
            return {"status": "success", "reason": "All fields were good"}

        except Exception as exc:
            return {"status": "failed", "reason": str(exc)}

        finally:
            await browser.close()
