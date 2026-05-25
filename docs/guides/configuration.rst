Configuration
=============

finamt reads configuration from environment variables or a ``.env`` file
placed in the working directory.

Environment variables
---------------------

All variables use the ``FINAMT_`` prefix and can be placed in a ``.env``
file in the working directory or exported to the shell environment.

**Extraction agents (4-agent pipeline)**

.. list-table::
   :header-rows: 1
   :widths: 35 25 40

   * - Variable
     - Default
     - Description
   * - ``FINAMT_AGENT_MODEL``
     - ``mistral:7b``
     - Model used by all four extraction agents. Supported values: ``mistral:7b``, ``qwen2.5:7b-instruct-q4_K_M``.
   * - ``FINAMT_AGENT_TIMEOUT``
     - ``60``
     - HTTP timeout in seconds per agent LLM call.
   * - ``FINAMT_AGENT_NUM_CTX``
     - ``4096``
     - Context window size (tokens) for agent LLM calls.
   * - ``FINAMT_AGENT_MAX_RETRIES``
     - ``2``
     - Retry attempts on failed agent LLM requests.

**OCR and PDF**

.. list-table::
   :header-rows: 1
   :widths: 35 25 40

   * - Variable
     - Default
     - Description
   * - ``FINAMT_OCR_TIMEOUT``
     - ``60``
     - Seconds to wait for PaddleOCR before falling back to Tesseract.
   * - ``FINAMT_TESSERACT_CMD``
     - ``tesseract``
     - Path to the Tesseract binary; useful when Tesseract is not on ``PATH``.
   * - ``FINAMT_PDF_DPI``
     - ``150``
     - DPI resolution used when rendering PDF pages to images.

**Data storage**

.. list-table::
   :header-rows: 1
   :widths: 35 25 40

   * - Variable
     - Default
     - Description
   * - ``FINAMT_PROJECT``
     - ``default``
     - Active project name; data is stored under ``~/.finamt/<project>/``.

``.env`` file example
---------------------

Copy ``env.example`` from the repository root and adjust it to your setup:

.. code-block:: ini

   FINAMT_AGENT_MODEL=mistral:7b
   FINAMT_AGENT_TIMEOUT=60
   FINAMT_OCR_TIMEOUT=60
   FINAMT_PROJECT=default

Programmatic configuration
---------------------------

Pass ``Config`` and / or ``AgentsConfig`` directly when constructing the agent:

.. code-block:: python

   from finamt import FinanceAgent, Config
   from finamt.agents.config import AgentsConfig

   config = Config(
       ocr_timeout=90,
       pdf_dpi=200,
   )

   agents_cfg = AgentsConfig(
       agent_model="qwen2.5:7b-instruct-q4_K_M",
       agent_timeout=90,
   )

   agent = FinanceAgent(config=config, agents_cfg=agents_cfg)

Data is stored under the named project directory by default:

.. code-block:: python

   # Use a custom project name (data stored at ~/.finamt/work/)
   agent = FinanceAgent(project="work")

   # Provide an explicit DB path
   agent = FinanceAgent(db_path="/data/myreceipts.db")

   # Disable persistence entirely
   agent = FinanceAgent(db_path=None)

Using a different model
-----------------------

Change the model by setting ``FINAMT_AGENT_MODEL`` or passing ``AgentsConfig``:

.. code-block:: python

   from finamt import FinanceAgent
   from finamt.agents.config import AgentsConfig

   agent = FinanceAgent(
       agents_cfg=AgentsConfig(agent_model="qwen2.5:7b-instruct-q4_K_M")
   )

The model is downloaded automatically on first use. Supported short names:

- ``mistral:7b`` — default; maps to ``mlx-community/Mistral-7B-Instruct-v0.3-4bit`` (Apple Silicon) or ``mistralai/Mistral-7B-Instruct-v0.3`` (other)
- ``qwen2.5:7b-instruct-q4_K_M`` — maps to ``mlx-community/Qwen2.5-7B-Instruct-4bit`` (Apple Silicon) or ``Qwen/Qwen2.5-7B-Instruct`` (other)