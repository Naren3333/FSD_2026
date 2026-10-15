import { Pipe, PipeTransform } from '@angular/core';
import type { Evidence } from '../api/models';
import { total } from './names';

/** An evaluation's marks as "awarded / possible". */
@Pipe({ name: 'marks', standalone: true })
export class MarksPipe implements PipeTransform {
  transform(evidence: readonly Evidence[]): string {
    const { awarded, possible } = total(evidence);
    return `${awarded} / ${possible}`;
  }
}
