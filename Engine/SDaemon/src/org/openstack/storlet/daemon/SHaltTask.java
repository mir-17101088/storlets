/*----------------------------------------------------------------------------
 * Copyright IBM Corp. 2015, 2015 All Rights Reserved
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * Limitations under the License.
 * ---------------------------------------------------------------------------
 */

/*============================================================================
 DD-MMM-YYYY    eranr       Initial implementation.
 10-Jul-2014    evgenyl     Refactoring.
 ===========================================================================*/
package org.openstack.storlet.daemon;

import org.slf4j.Logger;

/*----------------------------------------------------------------------------
 * SHaltTask
 * 
 * Instantiate AbstractTask class. The primary usage intent is to stop
 * a relevant working thread.
 * */
public class SHaltTask extends SAbstractTask {
    /*------------------------------------------------------------------------
     * CTOR
     * */
    public SHaltTask(Logger logger) {
        super(logger);
    };
}
/* ============================== END OF FILE =============================== */
