import base64
import os
import re
from typing import Any, Optional
from llama_index.core.llms import ChatMessage
from llama_index.core.workflow import (
    Context,
    Event,
    StartEvent,
    StopEvent,
    Workflow,
    step,
)
from llama_index.llms.openrouter import OpenRouter
from llama_cloud_services.parse import LlamaParse
from pydantic import BaseModel, Field


## Workflow Events


class LogEvent(Event):
    msg: str


class InputEvent(StartEvent):
    msg: str
    attachment: str | None = None
    file_name: str | None = None


class ParseEvent(Event):
    attachment: str
    file_name: str
    msg: str


class ChatEvent(Event):
    msg: str


class ChatResponseEvent(StopEvent):
    response: str
    citations: dict[int, list[str]]


## Structured Outputs


class Citation(BaseModel):
    """A citation to specific line(s) in the document."""

    citation_number: int = Field(
        description='The specific in-line citation number used in the response text.'
    )
    line_numbers: list[int] = Field(
        description='The line numbers in the document that are being cited.'
    )


class ChatResponse(BaseModel):
    """A response to the user with in-line citations (if any)."""

    response: str = Field(
        description='The response to the user including in-line citations (if any).'
    )
    citations: list[Citation] = Field(
        default=list,
        description='A list of citations, where each citation is an object to map the citation number to the line numbers in the document that are being cited.',
    )


class ParseAndChat(Workflow):
    def __init__(
        self,
        timeout: Optional[float] = None,
        verbose: bool = False,
        **workflow_kwargs: Any,
    ):
        super().__init__(timeout=timeout, verbose=verbose, **workflow_kwargs)
        self._llm = OpenRouter(
            model='anthropic/claude-3.5-haiku', 
            max_tokens=10000,
            api_key=os.getenv('OPENROUTER_API_KEY')
        )
        self._sllm = self._llm.as_structured_llm(ChatResponse)
        self._parser = LlamaParse(api_key=os.getenv('LLAMA_CLOUD_API_KEY'))
        self._system_prompt_template = """\
You are a helpful assistant that can answer questions about a document, provide citations, and engage in a conversation.

Here is the document with line numbers:
<document_text>
{document_text}
</document_text>

When citing content from the document:
1. Your in-line citations should start at [1] in every response, and increase by 1 for each additional in-line citation
2. Each citation number should correspond to specific lines in the document
3. If an in-line citation covers multiple sequential lines, do your best to prioritize a single in-line citation that covers the line numbers needed.
4. If a citation needs to cover multiple lines that are not sequential, a citation format like [2, 3, 4] is acceptable.
5. For example, if the response contains "The transformer architecture... [1]." and "Attention mechanisms... [2].", and these come from lines 10-12 and 45-46 respectively, then: citations = [[10, 11, 12], [45, 46]]
6. Always start your citations at [1] and increase by 1 for each additional in-line citation. DO NOT use the line numbers as the in-line citation numbers or I will lose my job.

IMPORTANT: Return a valid JSON response only. Do not include any control characters or special formatting.
"""

    def _clean_json_string(self, json_str: str) -> str:
        """Clean control characters from JSON string to ensure valid parsing."""
        # Remove control characters (0x00-0x1F) except for \t, \n, \r
        cleaned = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F]', '', json_str)
        return cleaned

    @step
    def route(self, ev: InputEvent) -> ParseEvent | ChatEvent:
        if ev.attachment:
            return ParseEvent(
                attachment=ev.attachment, file_name=ev.file_name, msg=ev.msg
            )
        else:
            return ChatEvent(msg=ev.msg)

    @step
    async def parse(self, ctx: Context, ev: ParseEvent) -> ChatEvent:
        ctx.write_event_to_stream(LogEvent(msg='Parsing document...'))
        results = await self._parser.aparse(
            base64.b64decode(ev.attachment),
            extra_info={'file_name': ev.file_name},
        )
        ctx.write_event_to_stream(LogEvent(msg='Document parsed successfully.'))

        documents = await results.aget_markdown_documents(split_by_page=False)

        # since we only have one document and are not splitting by page, we can just use the first one
        document = documents[0]

        # split the document into lines and add line numbers
        # this will be used for citations
        document_text = ''
        for idx, line in enumerate(document.text.split('\n')):
            document_text += f"<line idx='{idx}'>{line}</line>\n"

        await ctx.set('document_text', document_text)
        return ChatEvent(msg=ev.msg)

    @step
    async def chat(self, ctx: Context, event: ChatEvent) -> ChatResponseEvent:
        current_messages = await ctx.get('messages', default=[])
        current_messages.append(ChatMessage(role='user', content=event.msg))
        ctx.write_event_to_stream(
            LogEvent(
                msg=f'Chatting with {len(current_messages)} initial messages.'
            )
        )

        document_text = await ctx.get('document_text', default='')
        if document_text:
            ctx.write_event_to_stream(
                LogEvent(msg='Inserting system prompt...')
            )
            input_messages = [
                ChatMessage(
                    role='system',
                    content=self._system_prompt_template.format(
                        document_text=document_text
                    ),
                ),
                *current_messages,
            ]
        else:
            input_messages = current_messages

        try:
            response = await self._sllm.achat(input_messages)
            print(response)
            response_obj: ChatResponse = response.raw
        except Exception as e:
            # If structured output fails, try with regular LLM and manual parsing
            ctx.write_event_to_stream(
                LogEvent(msg='Structured output failed, trying fallback...')
            )
            try:
                fallback_response = await self._llm.achat(input_messages)
                raw_content = fallback_response.message.content
                
                # Clean the response before JSON parsing
                cleaned_content = self._clean_json_string(raw_content)
                
                # Try to extract JSON from the response
                import json
                response_obj = ChatResponse.model_validate_json(cleaned_content)
            except Exception as fallback_error:
                # Final fallback: create a simple response without structured output
                ctx.write_event_to_stream(
                    LogEvent(msg='All parsing failed, using simple response...')
                )
                fallback_response = await self._llm.achat(input_messages)
                response_obj = ChatResponse(
                    response=fallback_response.message.content,
                    citations=[]
                )
        
        ctx.write_event_to_stream(
            LogEvent(msg='LLM response received, parsing citations...')
        )

        current_messages.append(
            ChatMessage(role='assistant', content=response_obj.response)
        )
        await ctx.set('messages', current_messages)

        # parse out the citations from the document text
        citations = {}
        if document_text:
            for citation in response_obj.citations:
                line_numbers = citation.line_numbers
                for line_number in line_numbers:
                    start_idx = document_text.find(
                        f"<line idx='{line_number}'>"
                    )
                    end_idx = document_text.find(
                        f"<line idx='{line_number + 1}'>"
                    )
                    if start_idx >= 0:
                        citation_text = (
                            document_text[
                                start_idx
                                + len(f"<line idx='{line_number}'>") : end_idx
                            ]
                            .replace('</line>', '')
                            .strip()
                        )

                        if citation.citation_number not in citations:
                            citations[citation.citation_number] = []
                        citations[citation.citation_number].append(citation_text)

        return ChatResponseEvent(
            response=response_obj.response, citations=citations
        )


async def main():
    """Test script for the ParseAndChat agent."""
    agent = ParseAndChat()
    ctx = Context(agent)

    # run `wget https://arxiv.org/pdf/1706.03762 -O attention.pdf` to get the file
    # Or use your own file
    with open('attention.pdf', 'rb') as f:
        attachment = f.read()

    handler = agent.run(
        start_event=InputEvent(
            msg='Hello! What can you tell me about the document?',
            attachment=attachment,
            file_name='test.pdf',
        ),
        ctx=ctx,
    )

    async for event in handler:
        if not isinstance(event, StopEvent):
            print(event)

    response: ChatResponseEvent = await handler

    print(response.response)
    for citation_number, citation_texts in response.citations.items():
        print(f'Citation {citation_number}: {citation_texts}')

    # test context persistence
    handler = agent.run(
        'What was the last thing I asked you?',
        ctx=ctx,
    )
    response: ChatResponseEvent = await handler
    print(response.response)


if __name__ == '__main__':
    import asyncio

    asyncio.run(main())
