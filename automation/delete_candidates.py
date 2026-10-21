"""Text/link-only counterpart of the inventory scanner, dedicated to deletion."""
from selenium.webdriver.common.by import By


def extract_delete_candidates(driver):
    """Collect the same listing rows and links for deletion, without media lookups."""
    candidates = []

    try:
        rows = driver.execute_script(
            """
            const results = [];
            for (const checkbox of document.querySelectorAll('input[aria-label="Select Listing"]')) {
              let row = checkbox.parentElement;
              while (row && row !== document.body) {
                const more = row.querySelector('[role="button"][aria-label^="More options for "]');
                if (more) break;
                row = row.parentElement;
              }
              if (!row || row === document.body) continue;
              const controls = [...row.querySelectorAll('[role="button"][aria-label]')];
              const titleControl = controls.find(el => {
                const label = (el.getAttribute('aria-label') || '').trim();
                return label && !label.startsWith('More options for ');
              });
              const links = [...row.querySelectorAll('a[href]')].map(a => a.href).filter(Boolean);
              const href = links.find(value => value.includes('/marketplace/item/') || value.includes('listing_id'))
                || links.find(value => value.includes('/marketplace/')) || '';
              const rowText = row.innerText || '';
               results.push({
                 href,
                 title: titleControl ? (titleControl.getAttribute('aria-label') || '') : '',
                 text: rowText,
                 images: [],
                 element: titleControl || row,
                 source: 'full_row_dom'
               });
            }
            return results;
            """
        ) or []
        candidates.extend(item for item in rows if isinstance(item, dict))
    except Exception as exc:
        print(f"[DeleteListings] full-row DOM scan failed: {exc}", flush=True)

    anchors = driver.find_elements(
        By.XPATH,
        "//a[contains(@href,'/marketplace/item/') or contains(@href,'listing_id')]",
    )
    for anchor in anchors:
        try:
            if not anchor.is_displayed():
                continue
            href = anchor.get_attribute("href") or ""
            title = anchor.text or anchor.get_attribute("aria-label") or ""
            card_text = title
            images = []
            try:
                card = anchor.find_element(
                    By.XPATH,
                    "./ancestor::*[@role='article' or @role='button'][1]",
                )
                card_text = card.text or title
                images = []
            except Exception:
                pass
            candidates.append({"href": href, "title": title, "text": card_text, "click_text": "", "images": images, "element": anchor})
        except Exception:
            continue

    row_locator = (
        "//input[@aria-label='Select Listing']/ancestor::div["
        ".//*[@role='button' and starts-with(@aria-label,'More options for ')]]"
        "[1]"
    )
    for row in driver.find_elements(By.XPATH, row_locator):
        try:
            if not row.is_displayed():
                continue
            title_controls = row.find_elements(
                By.XPATH,
                ".//*[@role='button' and @aria-label and "
                "not(starts-with(@aria-label,'More options for '))]",
            )
            title = next(
                (str(control.get_attribute("aria-label") or "").strip()
                 for control in title_controls
                 if str(control.get_attribute("aria-label") or "").strip()),
                "",
            )
            href_values = [
                str(link.get_attribute("href") or "").strip()
                for link in row.find_elements(By.XPATH, ".//a[@href]")
            ]
            href_values.extend(
                str(row.get_attribute(name) or "").strip()
                for name in ("data-href", "data-url", "data-permalink")
            )
            href = next(
                (value for value in href_values if "/marketplace/item/" in value or "listing_id" in value),
                next((value for value in href_values if "/marketplace/" in value), ""),
            )
            images = []
            candidates.append({
                "href": href,
                "title": title,
                "text": row.text or "",
                "click_text": "",
                "images": images,
                "element": row,
            })
        except Exception:
            continue

    card_locator = (
        "//span[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz'), 'listed on')]/ancestor::*[@role='button'][1]"
    )
    for card in driver.find_elements(By.XPATH, card_locator):
        try:
            if not card.is_displayed():
                continue
            href = ""
            # Depending on the Marketplace layout the listing URL is either
            # an anchor inside the card or stored as a data/aria attribute on
            # the card itself. Prefer an actual marketplace item URL, then
            # fall back to the first usable Facebook marketplace link.
            links = card.find_elements(By.XPATH, ".//a[@href]")
            href_values = [str(link.get_attribute("href") or "").strip() for link in links]
            href_values.extend(
                str(card.get_attribute(name) or "").strip()
                for name in ("data-href", "data-url", "data-permalink", "aria-label")
            )
            href = next(
                (value for value in href_values if "/marketplace/item/" in value or "listing_id" in value),
                next((value for value in href_values if "/marketplace/" in value), ""),
            )
            images = []
            candidates.append({"href": href, "title": card.get_attribute("aria-label") or "", "text": card.text or "", "click_text": "", "images": images, "element": card})
        except Exception:
            continue

    return candidates
