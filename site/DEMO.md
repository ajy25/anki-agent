# App walkthrough

`public/demo.mp4` is a 17-second recording of the running Anki Agent 1.3.1 browser interface, recorded on 2026-11-07. It shows a keyword search, a live tutor reply from Groq's GPT-OSS 120B with an inline [1] citation, clicking that citation to highlight the matching source card, and the expanded Thinking panel. It is not a recreated interface or a scripted model response.

The recording uses an isolated collection named **Sample study deck**, containing six original cards about active recall, spaced repetition, Anki tags, and decks. No personal collection, API key field, or setup credentials appear in the recording. The source deck and search cache were separate from the user's own Anki files.

The video has no audio. `public/demo.vtt` supplies walkthrough captions, and the page includes a text description. `public/demo-poster.webp` is captured from the same app session.

To update the demo, record the current app with a separate sample collection and cache, using the real search and tutor behavior. Replace the MP4, poster, and captions together, and update this note with the app version and recording date. Keep the caption clear that the browser interface is shown. The Mac app uses the same study UI with native menus.

The tutor was asked to cite the cards it used. The citation was returned by the model, and its click/highlight behavior was verified in the running app; it was not added to the recording afterward. The original clip was replaced because its model reply displayed a source card but omitted inline citations.
