import fs from 'node:fs/promises';
import path from 'node:path';
import { SpreadsheetFile, Workbook } from '@oai/artifact-tool';

const outputDir = process.argv[2];
const outputName = process.argv[3] && !process.argv[3].startsWith('--')
  ? process.argv[3] : 'company_policy_import_blank_template.xlsx';
if (!outputDir) throw new Error('Usage: node build_2025_import_template.mjs output-dir');

// Policy-level details repeat; MemberCover is the individual's insured benefit.
const columns = [
  'Company', 'Branch', 'Region', 'Policy_Number', 'PolicyStatus',
  'ProductName', 'ProductCover', 'Principal_ID_Number', 'Surname', 'Initials',
  'Cell_Number', 'Email Address', 'Address', 'PremiumDue', 'Total',
  'PaymentMethod', 'LastDatePaid', 'Member_Relationship', 'Member_Name',
  'Member_ID_Number', 'MemberCover',
];
const workbook = Workbook.create();
const sheet = workbook.worksheets.add('POLICY_IMPORT');
sheet.showGridLines = false;
sheet.getRange('A1:U1').values = [columns];
sheet.getRange('A1:U1').format = {
  fill: '#374151', font: { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' },
};
sheet.getRange('A1:U1').format.rowHeight = 33;
sheet.getRange('A:U').format.columnWidth = 20;
for (const col of ['A', 'F', 'M', 'S']) sheet.getRange(`${col}:${col}`).format.columnWidth = 29;
sheet.getRange('L:L').format.columnWidth = 30;
sheet.getRange('D:D').format.columnWidth = 23;
sheet.getRange('H:H').format.columnWidth = 24;
sheet.getRange('T:T').format.columnWidth = 24;
for (const col of ['D', 'H', 'K', 'T']) sheet.getRange(`${col}2:${col}2000`).setNumberFormat('@');
for (const col of ['G', 'N', 'O', 'U']) sheet.getRange(`${col}2:${col}2000`).setNumberFormat('#,##0.00');
sheet.getRange('Q2:Q2000').setNumberFormat('dd/mm/yyyy');
sheet.getRange('E2:E2000').dataValidation = { rule: { type: 'list', values: ['Active', 'Lapsed', 'Cancelled'] } };
sheet.getRange('R2:R2000').dataValidation = { rule: { type: 'list', values: ['Principal', 'Spouse', 'Child', 'Extended', 'Extra'] } };
sheet.freezePanes.freezeRows(1);

workbook.recalculate();
const inspection = await workbook.inspect({ kind: 'table', range: 'POLICY_IMPORT!A1:U3',
  include: 'values,formulas', tableMaxRows: 3, tableMaxCols: 21 });
console.log(inspection.ndjson);
const errors = await workbook.inspect({ kind: 'match',
  searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',
  options: { useRegex: true, maxResults: 100 }, summary: 'formula error scan' });
console.log(errors.ndjson);
await fs.mkdir(outputDir, { recursive: true });
if (!process.argv.includes('--no-previews')) {
  const preview = await workbook.render({ sheetName: 'POLICY_IMPORT', range: 'A1:K8', scale: 1.2, format: 'png' });
  await fs.writeFile(path.join(outputDir, 'policy_import_preview.png'), new Uint8Array(await preview.arrayBuffer()));
}
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(path.join(outputDir, outputName));
console.log('Wrote', path.join(outputDir, outputName));
