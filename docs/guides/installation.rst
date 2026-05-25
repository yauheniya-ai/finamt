Installation
============

Requirements
------------

- Python 3.10 or newer
- Tesseract OCR *(optional — used as a fallback when PaddleOCR times out)*

.. note::

   **No Ollama required.** LLM models are downloaded automatically from HuggingFace on first
   use (~4 GB per model) and cached in ``~/.cache/huggingface/hub``. On Apple Silicon the
   ``mlx-lm`` backend is used; on other platforms ``transformers`` is used.

Install from PyPI
-----------------

.. code-block:: bash

   pip install finamt

For CLI usage, installing via `pipx <https://pipx.pypa.io/>`_ is recommended.
It places ``finamt`` into its own isolated virtual environment so its
dependencies never conflict with other projects, while still making the
``finamt`` command available system-wide without activating a virtualenv:

.. code-block:: bash

   pipx install finamt

Optional extras
~~~~~~~~~~~~~~~

Install development tools:

.. code-block:: bash

   pip install "finamt[dev]"

Install documentation tools:

.. code-block:: bash

   pip install "finamt[docs]"

Installing Tesseract (optional)
--------------------------------

.. code-block:: bash

   # macOS
   brew install tesseract tesseract-lang

   # Ubuntu / Debian
   sudo apt install tesseract-ocr tesseract-ocr-deu

Tesseract is only invoked if PaddleOCR fails or exceeds its timeout, so it is
not required for most use cases.
