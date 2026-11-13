# Find the cards you want to study

Open the **Search** tab. Choose **AI assist**, **Deep search**, or leave both off for keyword search. Turning on one AI option turns the other off.

| Option | What happens | When to use it |
| --- | --- | --- |
| Keyword search | The app matches words and tags locally. No AI call. | You know a drug name, diagnosis, phrase, or tag. |
| AI assist | AI turns your description into a keyword-and-tag search. The app returns the matching cards. | You know the topic but are unsure which terms or tags your deck uses. |
| Deep search | AI finds possible matches, then reviews short excerpts from those cards and keeps those it judges relevant. | You want cards meeting a specific criterion, or AI assist returns too many off-topic cards. |

These options find cards. For an explanation that cites your cards, use **Tutor**.

## Keyword search: start with a term you know

Leave both AI options off and enter something like `sucralfate`. You can also use a quoted phrase or a tag filter such as `tag:Pharmacology sucralfate`.

The app searches the text and tags it has indexed from your Anki collection. It does not ask a model to interpret your request or review the results. This is the quickest option when you know the wording you want.

## AI assist: let AI work out the search

Turn on **AI assist** and describe the cards you want. For example:

> Find cards about drugs that protect gastric ulcers.

1. **Interpret the request.** Your selected model chooses useful terms and concepts.
2. **Explore the deck.** It can look up or browse actual deck tags and try keyword searches to check possible matches. This can involve several AI calls.
3. **Build and run a search.** It chooses keywords, tag filters, and alternatives. The app runs that search and shows the matching cards, along with the search plan.

AI assist does not separately judge the relevance of every returned card. A result appears because it matches the final search's words or tags. If the results are too broad, make the request more specific or try Deep search. If no cards appear, try different wording or search a known term directly.

## Deep search: add a relevance check

Turn on **Deep search** and describe what should count as a match. For example:

> Find gastric ulcer treatments that protect the mucosa rather than suppress acid.

1. **Find possible matches.** AI builds a broader keyword-and-tag search, using alternative terms or related tags to gather candidates. If the initial search finds nothing, the app tries loosening its filters.
2. **Narrow a broad request.** With more than **120 candidates**, the app pauses. Add a detail, choose a suggested topic and click **Refine**, or choose **Screen all … anyway** to continue with the candidate set.
3. **Review excerpts.** The model sees short front-and-back excerpts and topic tags, in small batches. It decides which candidates fit your request.
4. **Show the kept cards.** Each retained result has a brief reason for inclusion. The results also show how many candidates were kept or screened.

Deep search reviews **at most 400 candidates per search**. It does not read every full card in the collection: excerpts can be truncated, and cards outside the retrieved candidate set are not screened. It can miss relevant cards or exclude a useful card, so an empty result is not proof that your deck lacks the topic. Try a broader description or a keyword search if needed.

The extra review uses more AI calls and can take longer or consume more API credits than AI assist. It aims to filter out off-topic matches; it does not guarantee a more complete result set.

## Inspect what happened

The **search plan** shows the keywords and tags used. Expand **Thinking** to see search activity, progress, and any readable reasoning returned by the model. Some models return no reasoning text. Deep search also shows its keep/exclude outcome through the result counts and each kept card's reason.

Both AI options use the model selected at the top of the app, within the collection chosen in Setup. Selecting a deck in the Search sidebar limits the final results to that deck and its subdecks. Questions, relevant deck text, and tag information may be sent to the service for the selected model; keyword search stays local.

For setup and model choices, see the [Mac guide](MAC_SETUP.md) and [OpenRouter guide](OPENROUTER.md).
