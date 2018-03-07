import React from 'react';
import classNames from 'classnames';


function CheckIcon({ success }) {
  return (
    <svg className={classNames('check-mark', { success })} xmlns="http://www.w3.org/2000/svg" width="8" height="8" viewBox="0 0 8 8">
      <path d="M6.41 0l-.69.72-2.78 2.78-.81-.78-.72-.72-1.41 1.41.72.72 1.5 1.5.69.72.72-.72 3.5-3.5.72-.72-1.44-1.41z" transform="translate(0 1)" />
    </svg>
  );
}

export default CheckIcon;
