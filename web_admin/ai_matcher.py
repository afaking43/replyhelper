"""
AI matching module using TF-IDF + cosine similarity.
"""
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


def find_matches(query: str, templates: list[dict], top_k: int = 10) -> list[dict]:
    """
    Find best matching templates for a query using TF-IDF + cosine similarity.
    Returns list of templates with 'score' field added.
    """
    if not query or not templates:
        return []

    query_lower = query.lower().strip()
    if not query_lower:
        return []

    docs = []
    valid_templates = []
    for tpl in templates:
        question = (tpl.get("question") or "").lower().strip()
        answer = (tpl.get("answer") or "").lower().strip()
        if question or answer:
            docs.append(f"{question} {answer}".strip())
            valid_templates.append(tpl)

    if not valid_templates:
        return []

    all_texts = [query_lower] + docs
    vectorizer = TfidfVectorizer()
    try:
        tfidf_matrix = vectorizer.fit_transform(all_texts)
    except ValueError:
        return []

    query_vec = tfidf_matrix[0:1]
    template_vecs = tfidf_matrix[1:]

    similarities = cosine_similarity(query_vec, template_vecs)[0]

    scored = []
    for i, score in enumerate(similarities):
        if score > 0.05:
            tpl_copy = valid_templates[i].copy()
            tpl_copy["score"] = float(score)
            scored.append(tpl_copy)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
