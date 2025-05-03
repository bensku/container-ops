from pyinfra.api import deploy

from containerops import nebula
from tests.nebula_common import net_config



@deploy('Test Nebula CA')
def make_ca():
    nebula.ca(net_config)


make_ca()