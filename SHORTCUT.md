# Movie Request iOS Shortcut Setup

These instructions will guide you through creating an iOS Shortcuts personal automation on your iPhone. This automation listens for incoming messages from a specific sender (your wife) and forwards them to your TrueNAS home server, then replies with the server's response.

## Creating the Automation

1. Open the **Shortcuts** app on your iPhone and go to the **Automations** tab.
2. Tap the **+** or **New Automation**.
3. Scroll down and tap on **Message**.
4. Set the following conditions:
   * **Sender**: Tap to choose your wife's contact.
   * **Message Contains**: Leave this empty.
   * Check **Run Immediately**.
   * Turn **Notify When Run** OFF (so it runs silently in the background).
5. Tap **Next** or **New Blank Automation** to begin adding actions.

## Adding the Actions

Add these actions in order by searching for the exact action names:

1. **Text**
   * Search for "Text" and add it.
   * In the text box, insert the **Shortcut Input** variable, tap it, and choose the **Content** property (the message text).
   * Later steps say "Text from step 1" for this action's output.

2. **Match Text**
   * Search for "Match Text".
   * Pattern: `(?i)^\s*&\s*(movie:.*|show:.*|\d{1,2}|more|cancel|undo|all|eta(\s+(all|\d))?|s(eason)?\s*\d{1,2})\s*$`
   * Text: the Text from step 1.

3. **If**
   * Search for "If".
   * Input: Matches (from the previous step).
   * Condition: **has any value**.
   * *(Note: In the "Otherwise" block, put a **Stop This Shortcut** action.)*

4. **Get Contents of URL**
   * Search for "Get Contents of URL". Place this inside the first part of the "If" block.
   * **URL**: `https://zaher.site/movie/message`
   * Tap the arrow/show more to expand options:
     * **Method**: POST
     * **Headers**: Add new header. Key: `X-Movie-Key`, Text: `<SECRET>` *(Replace `<SECRET>` with your actual secret key)*
     * **Request Body**: JSON
     * Add new field -> Text: Key `sender`, Value `wife`
     * Add new field -> Text: Key `text`, Value = the Text from step 1.

5. **Get Dictionary Value**
   * Search for "Get Dictionary Value".
   * **Key**: `reply`
   * **Dictionary**: Contents of URL (from the previous step).

6. **If**
   * Search for "If" again.
   * Input: Dictionary Value (from step 5).
   * Condition: **does not have any value**.
   * Action inside this If: **Stop This Shortcut**.

7. **Get Dictionary Value**
   * Search for "Get Dictionary Value".
   * **Key**: `image_url`
   * **Dictionary**: Contents of URL (from step 4).

8. **If**
   * Search for "If" again.
   * Input: Dictionary Value (from step 7).
   * Condition: **has any value**.
   * Inside this If, add:
     * **Get Contents of URL**: Input the Dictionary Value from step 7. (Method: GET).
     * **Send Message**: 
       * Message: Contents of URL (the downloaded image).
       * Recipients: Shortcut Input's Sender.
       * Expand options and turn **Show When Run** OFF.

9. **Send Message**
   * Place this at the end of the main "If" block (after step 8).
   * **Message**: Dictionary Value (from step 5, which is the text reply).
   * **Recipients**: Shortcut Input's Sender.
   * Expand options and turn **Show When Run** OFF.

10. Tap **Done** to save your automation.

---

## Testing & Troubleshooting

To test the automation:
1. Have your wife (or another Apple ID temporarily set as the sender in the automation) text you: `&movie: Scream` (or `&show: Severance`)
2. Test this while your phone is locked to ensure background execution works correctly.

**If nothing happens:**
* Double-check the Automation settings: Ensure **Run Immediately** is checked and **Notify When Run** is off.
* Cloudflare or firewall: Verify that Cloudflare or your home network isn't blocking the iPhone's request.
* Test your server directly from a terminal to confirm the TrueNAS app is working:

```bash
curl -s -X POST https://zaher.site/movie/message \
  -H "X-Movie-Key: <SECRET>" \
  -H "Content-Type: application/json" \
  -d '{"sender":"wife","text":"&movie: Scream"}'
```

---

## Cheat Sheet

You can text this to your wife to show her how to use the bot. Every command needs a `&` in front, so normal texts never trigger it:

* **&movie: <title>** - Search for a movie (e.g. `&movie: Scream`)
* **&show: <title>** - Search for a TV show (e.g. `&show: Severance`); results list how many seasons each has
* **&1**, **&2**, **&3** - Pick a movie or show from the search results
* **&all** - After picking a show, download every season you don't already have
* **&season N** (or **&sN**) - After picking a show, download just that season (e.g. `&s2`)
* **&more** - See more search results
* **&cancel** - Cancel the current search
* **&eta** - Check when your latest request will be ready (`&eta all` for the last 5, `&eta 2` for the one before)
* **&undo** - Undo the last request (within 3 minutes)

*(Note: There is a daily request limit (a show request counts as one), and search results expire after 15 minutes.)*
