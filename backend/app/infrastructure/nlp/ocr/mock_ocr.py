import asyncio
from .base import ExtractionResult

class MockOCRProvider:
    async def extract(self, file_path: str) -> ExtractionResult:
        await asyncio.sleep(2)  # simulate work

        text = """
                This Agreement is made effective as of January 1, 2026, between Alpha Corp ("Company") and John Doe ("Contractor"). 
                The Contractor agrees to provide software development services for a period of twelve (12) months. 
                Compensation shall be paid at a rate of $3,000 per month, due at the end of each billing cycle. 
                The Contractor shall maintain confidentiality of all proprietary information and may not disclose such information to any third party without prior written consent from the Company. 
                This Agreement may be terminated by either party with thirty (30) days written notice. 
                Upon termination, all outstanding payments must be settled within fifteen (15) days. 
                Renewal of this Agreement is optional and subject to mutual agreement by both parties.
           """


        return ExtractionResult(
            text=f"{text} {file_path}",
            page_count=1,
            meta={"engine": "mock"},
        )