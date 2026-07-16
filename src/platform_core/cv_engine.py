import os
from anthropic import Anthropic

class CaregiverAIEngine:
    def __init__(self):
        # Graceful fallback if key isn't provided yet
        api_key = os.getenv("ANTHROPIC_API_KEY", "mock_key")
        self.client = Anthropic(api_key=api_key) if api_key != "mock_key" else None

    def generate_tailored_cv(self, profile_summary: str, skills: str, job_description: str) -> str:
        if not self.client:
            return "AI Simulation Mode: Optimized CV Summary template matching UK Caregiving Framework standards."
        
        prompt = f"""
        You are an expert UK Healthcare recruiter. Optimize this candidate profile for an ATS tracking system.
        Candidate Profile: {profile_summary}
        Skills: {skills}
        Target Job Details: {job_description}
        
        Return a highly professional summary and optimized competency statements.
        """
        message = self.client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1500,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text