/**
 * SPIKE: "✨ AI" in the cell toolbar sends the whole cell text as a prompt.
 *
 * The button does no AI work itself. It puts `%%ai --replace` in front of the cell's text and
 * runs the cell; the kernel magic (spike/thebe_ai) calls the model and, through IPython's
 * set_next_input payload, replaces the cell with the generated code (the prompt kept as
 * `# ai:` comments). Keys never reach the browser, and the generated code is never run:
 * the user reads it and runs it with Shift+Enter.
 */
import { JupyterFrontEnd, JupyterFrontEndPlugin } from '@jupyterlab/application';
import { INotebookTracker, NotebookActions } from '@jupyterlab/notebook';

const COMMAND = 'thebe-ai:cell-prompt';
const MAGIC = /^%%ai\b/;

const plugin: JupyterFrontEndPlugin<void> = {
  id: 'thebe-ai-cell:plugin',
  description: 'Send the whole cell as a prompt to the %%ai magic (Thebe spike).',
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
          // Optional command args (e.g. from a second toolbar item): provider, "below" mode.
          const provider = typeof args.provider === 'string' ? ` ${args.provider}` : '';
          const where = args.mode === 'below' ? '' : ' --replace';
          cell.model.sharedModel.setSource(`%%ai${provider}${where}\n${source}`);
        }
        await NotebookActions.run(panel.content, panel.sessionContext);
      }
    });
  }
};

export default plugin;
