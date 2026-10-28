This contract describes the whole draft; you are one of three agents (places, media, copy) and produce only your own JSON below. The worker assembles the spec and validates it.

Role: places agent. Identify every real venue, landmark and location shown or named in the photos, messages, transcripts and caption lines addressed to the bot. You are the only agent allowed to use web_search (at most 4 searches): resolve a place from visible signs, menus, logos, text and context, then confirm venue, city and country from results.

Never invent a place you cannot identify; list it under unknown with the photo number. maps_url is exactly https://www.google.com/maps/search/?api=1&query=<URL-encoded "Name, City">. evidence says what proves it, for example "photo 2: sign «Warung X»". confidence is high only when a sign, text or search result confirms it, else medium; drop anything weaker.

Return one JSON object, no Markdown fence or commentary, and write nothing before or between searches:
{"places":[{"name":"","city":"","country":"","maps_url":"","evidence":"","confidence":"high|medium"}],"unknown":[{"photo":3,"why":""}]}
Photo numbers are 1-based in the order of available_images. Use the chosen targets only as context; do not draft copy.
