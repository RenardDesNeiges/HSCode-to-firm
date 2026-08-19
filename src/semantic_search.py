from sentence_transformers import SentenceTransformer
import pandas as pd
import numpy as np

class SemanticSearch:
    def __init__(self, 
                 model_name = "sentence-transformers/all-MiniLM-L6-v2", 
                 product_codes="data/HSCodeandDescription.xlsx"):
        self.model = SentenceTransformer(model_name)
        self.model.to("mps")

        self.hscodes = pd.read_excel(product_codes,sheet_name='HS22')
        self.hscodes4digits = self.hscodes[self.hscodes['Level']==4]
        self.hscodes4digits = self.hscodes4digits[['Code','Description']]
        self.hscodes4digits.index = self.hscodes4digits['Code']
        self.hscodes2digits = self.hscodes[self.hscodes['Level']==2]
        self.hscodes2digits = self.hscodes2digits[['Code','Description']]
        self.hscodes2digits.index = self.hscodes2digits['Code']

        def code_to_hs_chapt_desc(c):
            _2_digits = (str(c).zfill(4)[:2])
            return self.hscodes2digits.loc[_2_digits]['Description']

        self.hscodes4digits['Chapter'] = [code_to_hs_chapt_desc(c) for \
                                          c in self.hscodes4digits['Code']]

        self.hscodes4digits['EDescription'] = \
            "Description : " + self.hscodes4digits['Description'] + \
                "; Chapter : " + self.hscodes4digits['Chapter']
        self.hscodes4digits.index = range(len(self.hscodes4digits))
        self.products_embeddings = \
                self.model.encode(self.hscodes4digits['EDescription'].tolist())



    def chunk_text(self, text, chunk_size=7000, overlap=500):
        return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size-overlap)]

    def get_candidates(self, products_description, candidate_count=100):
        """ Given a natural language description of products, returns a list of candidates HS codes (based on semantic similarity)
        """
        filing_chunks_embeddings = self.model.encode(self.chunk_text(products_description))
        similarities = self.model.similarity(filing_chunks_embeddings, self.products_embeddings).numpy()
        sim_codes = self.hscodes4digits
        sim_codes['similarities'] = np.max(similarities,0)
        candidates = sim_codes.sort_values(by='similarities', ascending=False)[:candidate_count][['Code','Description']]
        return candidates
