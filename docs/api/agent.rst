Agent
=====

The :class:`~finanzamt.FinanceAgent` is the main entry point for processing
receipts. Internally it runs a 4-agent pipeline:

1. **Metadata agent** — document date, type, currency
2. **Counterparty agent** — vendor / client identification
3. **Amounts agent** — total, net, VAT, tax rate
4. **Line items agent** — individual purchased items

finanzamt.agents.agent
----------------------

.. automodule:: finanzamt.agents.agent
   :members:
   :undoc-members:
   :show-inheritance:

finanzamt.agents.config
-----------------------

.. automodule:: finanzamt.agents.config
   :members:
   :undoc-members:
   :show-inheritance:

finanzamt.agents.pipeline
-------------------------

.. automodule:: finanzamt.agents.pipeline
   :members:
   :undoc-members:
   :show-inheritance:

finanzamt.agents.prompts
------------------------

.. automodule:: finanzamt.agents.prompts
   :members:
   :undoc-members:
   :show-inheritance:

finanzamt.agents.llm\_caller
-----------------------------

.. automodule:: finanzamt.agents.llm_caller
   :members:
   :undoc-members:
   :show-inheritance:
