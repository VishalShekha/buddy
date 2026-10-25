import chromadb
from sentence_transformers import SentenceTransformer

client = chromadb.PersistentClient(path="./chroma_data")
collection = client.get_or_create_collection("personal_memory")
embedder = SentenceTransformer("all-MiniLM-L6-v2")  # small, runs fine on CPU

def add_memory(text: str, doc_id: str):
    emb = embedder.encode([text])[0].tolist()
    collection.add(ids=[doc_id], embeddings=[emb], documents=[text])

def query_memory(text: str, n: int = 5):
    emb = embedder.encode([text])[0].tolist()
    res = collection.query(query_embeddings=[emb], n_results=n)
    return res["documents"][0]

