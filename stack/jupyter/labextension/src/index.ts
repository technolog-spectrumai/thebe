/**
 * "✨ AI" in the notebook cell toolbar (and Ctrl+Alt+G): the cell's text is the request.
 *
 * The button does no AI work itself and never sees an API key. It puts `%%ai --replace` in
 * front of the cell's text and runs the cell; the %%ai magic in the kernel (thebe_ai.py) asks
 * the AI gateway, which counts the tokens against the budget, and replaces the cell with the
 * generated code through IPython's set_next_input payload (the request kept as `# ai:`
 * comments on top). The generated code is not run: the user reads it and presses Shift+Enter.
 * A cell that already starts with %%ai is run as it is, so its options apply.
 */
import { JupyterFrontEnd, JupyterFrontEndPlugin } from '@jupyterlab/application';
import { INotebookTracker, NotebookActions } from '@jupyterlab/notebook';

const COMMAND = 'thebe-ai:cell-prompt';
const MAGIC = /^%%ai\b/;

const plugin: JupyterFrontEndPlugin<void> = {
  id: 'thebe-ai-cell:plugin',
  description: 'Send the cell text as a request to the %%ai magic.',
  autoStart: true,
  requires: [INotebookTracker],
  activate: (app: JupyterFrontEnd, tracker: INotebookTracker) => {
    app.commands.addCommand(COMMAND, {
      label: 'Generate Code from This Cell (AI)',
      caption: 'Send the cell text to the AI and replace it with the generated code. Nothing is run.',
      isEnabled: () => tracker.activeCell?.model.type === 'code',
      execute: async args => {
        const panel = tracker.currentWidget;
        const cell = tracker.activeCell;
        if (!panel || !cell || cell.model.type !== 'code') {
          return;
        }
        const source = cell.model.sharedModel.getSource();
        if (!source.trim()) {
          return;
        }
        if (!MAGIC.test(source)) {
          // Optional command arguments (e.g. from a second toolbar item): a provider name, and
          // mode "below" to insert a new cell instead of replacing this one.
          const provider = typeof args.provider === 'string' && /^[a-z][a-z0-9_-]{0,31}$/.test(args.provider)
            ? ` ${args.provider}`
            : '';
          const where = args.mode === 'below' ? '' : ' --replace';
          cell.model.sharedModel.setSource(`%%ai${provider}${where}\n${source}`);
        }
        await NotebookActions.run(panel.content, panel.sessionContext);
      }
    });
  }
};

export default plugin;
