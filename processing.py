import os
import time
import hashlib
import numpy as np
import google.generativeai as genai
from PyPDF2 import PdfReader
from pdf2image import convert_from_path
import pytesseract
from sentence_transformers import SentenceTransformer
import faiss
from query import QueryBuilder


#MONGO DB (vector database)
#Langchain
#flowwise

class VectorStore:
    """Handles document embeddings and semantic search"""
    _model = None  # Singleton embedding model
    
    def __init__(self):
        if VectorStore._model is None:
            VectorStore._model = SentenceTransformer('all-MiniLM-L6-v2')
        self.index = faiss.IndexFlatL2(384)
        self.metadata = []

    def add_document(self, text, metadata):
        embeddings = self._model.encode(text)
        if len(embeddings.shape) == 1:
            embeddings = embeddings.reshape(1, -1)
        self.index.add(embeddings)
        self.metadata.extend([metadata]*len(embeddings))

    def search(self, query, k=5):
        query_embedding = self._model.encode(query).reshape(1, -1)
        distances, indices = self.index.search(query_embedding, k)
        return [(self.metadata[idx], float(distances[0][i])) 
                for i, idx in enumerate(indices[0])]

class PDFProcessor:
    """Handles PDF text extraction with OCR fallback"""
    
    def __init__(self, tesseract_cmd=r'/usr/bin/tesseract'):
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    def _extract_with_ocr(self, pdf_path, page_num):
        try:
            images = convert_from_path(pdf_path, first_page=page_num+1, last_page=page_num+1)
            return pytesseract.image_to_string(images[0])
        except Exception as e:
            print(f"OCR failed for {pdf_path} page {page_num}: {str(e)}")
            return ""

    def process_page(self, page, pdf_path, page_num):
        try:
            text = page.extract_text()
            if len(text) < 200:  # Heuristic for image-based page
                return self._extract_with_ocr(pdf_path, page_num)
            return text
        except Exception as e:
            return self._extract_with_ocr(pdf_path, page_num)

    def process_pdf(self, file_path):
        """Process a single PDF file"""
        doc_id = hashlib.md5(os.path.basename(file_path).encode()).hexdigest()[:8]
        contents = []
        
        with open(file_path, "rb") as f:
            reader = PdfReader(f)
            for page_num, page in enumerate(reader.pages):
                text = self.process_page(page, file_path, page_num)
                contents.append({
                    'text': text,
                    'metadata': {
                        'doc_id': doc_id,
                        'filename': os.path.basename(file_path),
                        'page': page_num + 1
                    }
                })
        return contents

class NotesGenerator:
    """Handles note generation using Gemini API"""    
    def __init__(self, api_key, all_topics):
        self.all_topics = all_topics
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            generation_config={
                "temperature": 0.75,
                "top_p": 0.85,
                "top_k": 30,
                "max_output_tokens": 8192,   # ↓ half of 8192
            }
        )

        self.SYSTEM_PROMPT = f"""Act as an **expert note-taker and tutor**. Your task is to create **detailed, self-contained notes** from the provided source material and your own knowledge that:
1. **Teach core concepts**
2. **Give better understanding to the readers**
3. **Exam ready**
4. **Easy for humans to read (Well structured and other things to imporve readablity).
6. **Use standard MarkDown format to answer the questions**

**Follow these rules:**  
1. **Structure the Notes**:  
   As specified by the user.
   Directly start off with the answer, no need to tell things lik "Ok i understand ..." or something like that.

2. **Audience**: Notes are for a learner who wants to understand the topics while also going into depth only where required. Prioritize depth and clarity.  

3. **Style**:  
   - Avoid summaries or TL;DR sections.  
   - Use bold for key terms and italics for examples.
   - Use tables, examples where necessarty.

4. **Answer Readiness**:  
   - If the source does not have complete information for the explaination, then you may use you own knowledge to fill the gaps and also to structure the notes."""  

    def _build_context(self, relevant_passages):
        context = "RELEVANT PASSAGES:\n"
        for (meta, score) in relevant_passages:
            context += f"\nDocument: {meta['filename']} (Page {meta['page']})\n"
            context += f"Content: {meta['doc_text']}\n"
            context += f"Relevance Score: {score:.2f}\n{'-'*40}"
        return context

    def generate_notes(self, vector_store, base_prompt, topics, max_retries: int = 3):
        """
        Generate notes for multiple topics, retrying if the LLM returns an empty response.
        """
        self.max_queries = 10
        queryBuilder = QueryBuilder()
        queries = queryBuilder.get_and_process_query("- " + "- ".join(topics), self.max_queries)

        # Build the overall context
        context = ""
        for query in queries[:self.max_queries]:
            relevant_passages = vector_store.search(query, k=(self.max_queries // len(queries)))
            context += self._build_context(relevant_passages)
        
        # Prepare the chat
        chat = self.model.start_chat(history=[])
        prompt = (
            f"{self.SYSTEM_PROMPT}\n\n"
            f"Topics:\n" + "\n".join(f"- {t}" for t in topics) + "\n\n"
            f"Context:\n{context}\n\n"
            f"Notes structure instructions:\n{base_prompt}"
        )

        # Retry loop
        for attempt in range(1, max_retries + 1):
            response = chat.send_message(prompt)
            try:
                return [response.text]
            except Exception as e:
                print("Error : (Processing.py[157])", e, sep = '\n')

        # All retries exhausted
        raise RuntimeError(
            f"LLM failed to return any content after {max_retries} attempts."
        )