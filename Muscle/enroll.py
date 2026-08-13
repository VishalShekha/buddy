import cv2
from insightface.app import FaceAnalysis
import numpy as np

app = FaceAnalysis(name="buffalo_l", providers=["CUDAExecutionProvider"])
app.prepare(ctx_id=0)

# put a few clear photos of yourself in a folder called reference_photos/
import glob
embeddings = []
for path in glob.glob("reference_photos/*.jpg"):
    img = cv2.imread(path)
    faces = app.get(img)
    if faces:
        embeddings.append(faces[0].normed_embedding)

np.save("known_faces.npy", np.array(embeddings))
print(f"Enrolled {len(embeddings)} reference faces.")