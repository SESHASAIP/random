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
# Helper: detect whether a field is a lookup (hyperlink) or plain text
# ---------------------------------------------------------------------------
async def _is_lookup_field(field_container) -> bool:
    """Return True if the field container holds a lookup link (anchor tag)."""
    link = field_container.locator("a")
    return await link.count() > 0


async def _get_field_current_value(page: Page, label: str):
    """
    Locate a field by its label text and return (container, current_value, is_lookup).
    """
    # Dynamics 365 typically renders fields near their label.
    # Try to find the field container associated with the label.
    field_container = page.locator(
        f"div[data-id='{label.lower().replace(' ', '_')}-field-container'], "
        f"div:has(> label:text-is('{label}'))"
    ).first

    is_lookup = await _is_lookup_field(field_container)

    if is_lookup:
        link = field_container.locator("a").first
        current_value = (await link.text_content() or "").strip()
    else:
        input_el = field_container.locator("input, textarea, select").first
        current_value = (await input_el.input_value()).strip() if await input_el.count() > 0 else ""
        # Fallback: maybe it is displayed as text
        if not current_value:
            current_value = (await field_container.locator("[data-id$='.fieldControl-text-box-text']").text_content() or "").strip()

    return field_container, current_value, is_lookup


async def _update_lookup_field(field_container, correct_value: str) -> None:
    """Clear a lookup field via its × button, search with wildcards, and select."""
    # Click the × (clear / delete) button on the existing lookup value
    clear_btn = field_container.locator(
        "button[data-id$='.fieldControl-LookupResultsDropdown_clear'], "
        "button:has(span:text('×')), "
        "button[aria-label='Clear']"
    ).first
    await clear_btn.click()
    await asyncio.sleep(0.5)

    # Type the value wrapped in asterisks into the lookup search box
    search_input = field_container.locator("input").first
    await search_input.fill(f"*{correct_value}*")
    await asyncio.sleep(2)  # wait for lookup results to load

    # Select the matching result from the dropdown
    result_item = field_container.page.locator(
        f"ul[aria-label='Lookup results'] li:has-text('{correct_value}')"
    ).first
    await result_item.click()
    await asyncio.sleep(0.5)


async def _update_plain_field(field_container, correct_value: str) -> None:
    """Clear a plain text / dropdown field and type the correct value."""
    input_el = field_container.locator("input, textarea").first
    if await input_el.count() > 0:
        await input_el.click()
        await input_el.fill("")
        await input_el.fill(correct_value)
    else:
        # Try select / dropdown
        select_el = field_container.locator("select").first
        if await select_el.count() > 0:
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
                    container, current_value, is_lookup = await _get_field_current_value(
                        page, field_label
                    )

                    if current_value == expected_value:
                        # Value matches → skip
                        continue

                    # Value does not match → update
                    if is_lookup:
                        await _update_lookup_field(container, expected_value)
                    else:
                        await _update_plain_field(container, expected_value)

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
                    container, current_value, is_lookup = await _get_field_current_value(
                        page, email_field_label
                    )
                    if current_value != "Server-Side Synchronization":
                        if is_lookup:
                            await _update_lookup_field(container, "Server-Side Synchronization")
                        else:
                            await _update_plain_field(container, "Server-Side Synchronization")
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
