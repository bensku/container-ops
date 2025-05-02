ENV['VAGRANT_DEFAULT_PROVIDER'] = 'libvirt'

Vagrant.configure("2") do |config|
  NUM_VMS = 3

  # Base box image for openSUSE MicroOS
  BOX_IMAGE = "https://download.opensuse.org/tumbleweed/appliances/openSUSE-MicroOS.x86_64-ContainerHost-Vagrant.box"

  IP_PREFIX = "10.1.57."

  # --- Define VMs using a loop ---
  (1..NUM_VMS).each do |i|
    vm_name = "containerops-#{i}"
    ip_address = "#{IP_PREFIX}#{10 + i}"

    config.vm.define vm_name do |node|
      # Set the box for this specific VM
      node.vm.box = "opensuse/MicroOS.x86_64"
      node.vm.box_url = BOX_IMAGE

      config.vm.synced_folder ".", "/vagrant", disabled: true

      node.vm.provider :libvirt do |domain|
        domain.memory = 1024
        domain.cpus = 2
      end
    end
  end
end