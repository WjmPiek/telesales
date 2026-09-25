import fs from 'node:fs/promises';
import path from 'node:path';
import { SpreadsheetFile, Workbook } from '@oai/artifact-tool';

const outputDir = process.argv[2];
const outputName = process.argv[3] && !process.argv[3].startsWith('--')
  ? process.argv[3] : 'company_policy_import_blank_template.xlsx';
if (!outputDir) throw new Error('Usage: node build_2025_import_template.mjs output-dir');

const workbook = Workbook.create();
const guide = workbook.worksheets.add('READ_ME');
const clients = workbook.worksheets.add('CLIENTS');
const members = workbook.worksheets.add('COVERED_MEMBERS');
for (const sheet of [guide, clients, members]) sheet.showGridLines = false;

guide.getRange('A2').values = [['Company policy import — blank template']];
guide.getRange('A4:B11').values = [
  ['Sheet / field', 'What to enter'],
  ['CLIENTS', 'One row per existing policy/client. Company and Branch identify the company database and reporting scope. Policy_Number must be the real policy reference.'],
  ['COVERED_MEMBERS', 'One row for EVERY covered person on that policy: principal, spouse, each child, and each extended member. Repeat Company, Branch, and Policy_Number.'],
  ['ID_Number', 'A valid 13-digit South African ID number for every covered person. Keep it as text to preserve leading zeroes. A date of birth alone cannot count toward cover or activate a policy.'],
  ['Cover_Amount', 'The insured benefit for THAT PERSON on THAT POLICY, not the premium or a family total. Use the product age-band benefit where applicable.'],
  ['Status', 'Active, Lapsed, or Cancelled. Only Active member rows count toward the system-wide cover ceiling. Verify status against the company policy register.'],
  ['Company / Branch', 'Use the same company name and branch spelling on both sheets. This separates company reports while allowing the parent company to check total cover across all companies.'],
  ['Import', 'Upload at Insurance Sales Lead Queue only after all required fields are complete. Import validates the entire member sheet before saving. Do not fill this template with product-catalogue rows.'],
];
guide.getRange('A2:B2').format.font = { name: 'Arial', size: 14, bold: true, color: '#1F2937' };
guide.getRange('A4:B11').format.font = { name: 'Arial', size: 10, color: '#1F2937' };
guide.getRange('A4:B4').format = { fill: '#374151', font: { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' } };
guide.getRange('A5:A11').format.font = { name: 'Arial', size: 10, bold: true, color: '#1F2937' };
guide.getRange('A:A').format.columnWidth = 24;
guide.getRange('B:B').format.columnWidth = 76;
guide.getRange('B5:B11').format.wrapText = true;
guide.getRange('A4:B11').format.rowHeight = 49;

clients.getRange('A1:M1').values = [[
  'Company', 'Branch', 'Policy_Number', 'ID_Number', 'Surname', 'Initials',
  'Cell_Number', 'Email Address', 'Address', 'PremiumDue', 'Total',
  'PaymentMethod', 'LastDatePaid',
]];
clients.getRange('A1:M1').format = { fill: '#374151', font: { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' } };
clients.getRange('A1:M1').format.rowHeight = 29;
clients.getRange('A:M').format.columnWidth = 20;
clients.getRange('A:A').format.columnWidth = 29;
clients.getRange('H:H').format.columnWidth = 31;
clients.getRange('I:I').format.columnWidth = 40;
clients.getRange('C2:D2000').setNumberFormat('@');
clients.getRange('G2:G2000').setNumberFormat('@');
clients.getRange('J2:K2000').setNumberFormat('#,##0.00');
clients.freezePanes.freezeRows(1);

members.getRange('A1:H1').values = [[
  'Company', 'Branch', 'Policy_Number', 'ID_Number', 'Cover_Amount',
  'Status', 'Relationship', 'ProductName',
]];
members.getRange('A1:H1').format = { fill: '#374151', font: { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' } };
members.getRange('A1:H1').format.rowHeight = 29;
members.getRange('A:H').format.columnWidth = 22;
members.getRange('A:A').format.columnWidth = 29;
members.getRange('H:H').format.columnWidth = 48;
members.getRange('C2:D2000').setNumberFormat('@');
members.getRange('E2:E2000').setNumberFormat('#,##0.00');
members.getRange('F2:F2000').dataValidation = { rule: { type: 'list', values: ['Active', 'Lapsed', 'Cancelled'] } };
members.freezePanes.freezeRows(1);

workbook.recalculate();
await fs.mkdir(outputDir, { recursive: true });
for (const [sheetName, range, filename] of process.argv.includes('--no-previews') ? [] : [
  ['READ_ME', 'A2:B11', 'read_me_preview.png'],
  ['CLIENTS', 'A1:M8', 'clients_preview.png'],
  ['COVERED_MEMBERS', 'A1:H8', 'covered_members_preview.png'],
]) {
  const preview = await workbook.render({ sheetName, range, scale: 1.2, format: 'png' });
  await fs.writeFile(path.join(outputDir, filename), new Uint8Array(await preview.arrayBuffer()));
}
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(path.join(outputDir, outputName));
console.log('Wrote', path.join(outputDir, outputName));
